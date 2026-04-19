"""Dashboard directive + halt endpoints -- ARCH-H4 (#159).

Three intervention modes converge here (see diagram 10):

1. **Reactive gate L4** -- existing approval-gate path (``/agui/approve``),
   unchanged by this module.
2. **Halt-anywhere button** (``POST /api/halt``) -- sets a disk-backed
   ``halt_requested`` flag. The approval-gate poll loop reads this flag on
   every tick and blocks until the flag clears, so the pipeline pauses at
   the next safe checkpoint rather than aborting an in-flight tool call.
3. **Direct directive injection** (``POST /api/directive``) -- parses a
   free-form reviewer directive through the ARCH-A2 Preference Interpreter
   (landed via PR #171), appends the resulting records to the Preference
   Ledger (ARCH-A1), runs the ARCH-A5 consistency check, and hands any
   resulting drift signals to the ARCH-A6 re-manifestation handler. The
   pipeline keeps running; the new records just drift the relevant stages.

Design invariants (per issue #159):

* **No mutation bypasses the ledger.** Halt + directive both flow through
  A1/A2. This module never edits artifacts directly.
* **Halt is advisory at next safe checkpoint.** Setting the flag never
  aborts a running tool call; the approval-gate loop reads it and pauses
  when workers finish their in-flight call.
* **Scoped-by-default.** When ``slot_context`` is supplied, the reviewer
  had a slot selected on the dashboard; the slot's scene / voice-block /
  clip id is passed as the Preference Interpreter's ``scope_hint``.
* **Fail loud.** ``InterpreterError`` -> HTTP 422 with the failure reason;
  append / consistency failures -> HTTP 500 (pipeline-invariant violation,
  not a reviewer mistake).

Shared state: the endpoints operate on a disk-backed "dashboard
blackboard" JSON file next to the existing approval state. This mirrors
the IPC pattern already used by :mod:`callbacks.approval_gate` and lets
a separately-spawned pipeline process observe the directives and the
halt flag without any in-process coupling.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Mapping, MutableMapping, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agents.preference_interpreter import (
    InterpreterError,
    interpret_directive,
)
from callbacks.consistency_checker import (
    LEDGER_DRIFT_SIGNALS_KEY,
    check_consistency_at_gate,
    pending_drift_signals,
)
from callbacks.preference_ledger import PREFERENCE_LEDGER_KEY
from callbacks.remanifestation import DriftHandlingReceipt, handle_drift

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard-directives"])


# ---------------------------------------------------------------------------
# Disk-backed shared state
# ---------------------------------------------------------------------------

_OUTPUT_DIR = os.environ.get("PIPELINE_OUTPUT_DIR", "/tmp/documentary-pipeline")
_HALT_FILE = os.path.join(_OUTPUT_DIR, ".halt_state.json")
_BLACKBOARD_FILE = os.path.join(_OUTPUT_DIR, ".dashboard_blackboard.json")

#: Default halt payload when no state has ever been written.
_HALT_DEFAULT: dict[str, Any] = {
    "halt_requested": False,
    "halted_at_stage": None,
    "halt_reviewer": None,
    "halt_reason": None,
    "halt_timestamp": None,
}

#: Serialises write-then-read cycles on the shared blackboard. The lock is
#: process-local so cross-process callers still rely on the atomic
#: JSON-file replacement below; the lock protects the endpoint's own
#: read-parse-append-write cycle against concurrent HTTP requests.
_state_lock = threading.Lock()


def _atomic_write_json(path: str, payload: Any) -> None:
    """Write ``payload`` as JSON to ``path`` atomically.

    Writes to a sibling ``.tmp`` file and ``os.replace``-s into place so
    readers (the pipeline's approval-gate poll loop, other endpoint
    invocations) never observe a half-written file.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Halt state
# ---------------------------------------------------------------------------


def _read_halt_state() -> dict[str, Any]:
    """Load the halt state, falling back to the default when absent."""
    if not os.path.exists(_HALT_FILE):
        return dict(_HALT_DEFAULT)
    try:
        with open(_HALT_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("halt-state file %s unreadable; resetting", _HALT_FILE)
        return dict(_HALT_DEFAULT)
    if not isinstance(data, dict):
        logger.warning("halt-state file %s malformed; resetting", _HALT_FILE)
        return dict(_HALT_DEFAULT)
    merged = dict(_HALT_DEFAULT)
    merged.update(data)
    return merged


def _write_halt_state(state: Mapping[str, Any]) -> None:
    _atomic_write_json(_HALT_FILE, dict(state))


def is_halt_requested() -> bool:
    """Return ``True`` iff the halt button is currently engaged.

    Called from :func:`callbacks.approval_gate.wait_for_approval` on every
    poll tick.
    """
    return bool(_read_halt_state().get("halt_requested", False))


def set_halt_requested(
    *,
    reviewer: Optional[str] = None,
    reason: Optional[str] = None,
    at_stage: Optional[str] = None,
) -> dict[str, Any]:
    """Engage the halt flag.  Returns the new halt state."""
    state = _read_halt_state()
    state["halt_requested"] = True
    if reviewer is not None:
        state["halt_reviewer"] = reviewer
    if reason is not None:
        state["halt_reason"] = reason
    if at_stage is not None:
        state["halted_at_stage"] = at_stage
    state["halt_timestamp"] = time.time()
    _write_halt_state(state)
    return state


def clear_halt() -> dict[str, Any]:
    """Release the halt flag so the gate-poll loop can resume."""
    state = dict(_HALT_DEFAULT)
    _write_halt_state(state)
    return state


def mark_halted_at_stage(stage_name: str) -> None:
    """Record the stage the pipeline paused at after observing the flag.

    Called by the approval-gate poll loop the first time it observes the
    halt flag. No-op when the flag is not set.
    """
    if not stage_name:
        return
    state = _read_halt_state()
    if not state.get("halt_requested"):
        return
    if state.get("halted_at_stage") == stage_name:
        return
    state["halted_at_stage"] = stage_name
    state["halt_timestamp"] = time.time()
    _write_halt_state(state)


# ---------------------------------------------------------------------------
# Dashboard blackboard (shared ledger-bearing state)
# ---------------------------------------------------------------------------


def _empty_blackboard() -> dict[str, Any]:
    return {
        PREFERENCE_LEDGER_KEY: "[]",
        LEDGER_DRIFT_SIGNALS_KEY: "[]",
    }


def _load_blackboard() -> dict[str, Any]:
    if not os.path.exists(_BLACKBOARD_FILE):
        return _empty_blackboard()
    try:
        with open(_BLACKBOARD_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "dashboard blackboard %s unreadable; resetting",
            _BLACKBOARD_FILE,
        )
        return _empty_blackboard()
    if not isinstance(data, dict):
        logger.warning(
            "dashboard blackboard %s malformed; resetting",
            _BLACKBOARD_FILE,
        )
        return _empty_blackboard()
    # Ensure the ledger and drift queue keys exist so append_preference +
    # pending_drift_signals never KeyError on a freshly-restored file.
    if PREFERENCE_LEDGER_KEY not in data:
        data[PREFERENCE_LEDGER_KEY] = "[]"
    if LEDGER_DRIFT_SIGNALS_KEY not in data:
        data[LEDGER_DRIFT_SIGNALS_KEY] = "[]"
    return data


def _save_blackboard(state: Mapping[str, Any]) -> None:
    _atomic_write_json(_BLACKBOARD_FILE, dict(state))


# ---------------------------------------------------------------------------
# Slot context -> scope hint
# ---------------------------------------------------------------------------


# Recognised slot-context keys, in priority order. The first key present on
# the payload wins -- narrower scopes (clip -> voice_block -> scene -> stage)
# take precedence so a fully-qualified slot maps to the tightest scope.
_SLOT_SCOPE_KEYS: tuple[tuple[str, str], ...] = (
    ("clip_id", "element"),
    ("element_id", "element"),
    ("voice_block_id", "voice_block"),
    ("block_id", "voice_block"),
    ("speaker", "voice_block"),
    ("scene_id", "scene"),
    ("scene_num", "scene"),
    ("stage", "stage"),
    ("stage_name", "stage"),
    ("artifact_type", "artifact_type"),
)


def _slot_context_to_scope_hint(
    slot_context: Optional[Mapping[str, Any]],
) -> Optional[dict[str, Any]]:
    """Translate a dashboard slot-context payload into an A2 scope hint.

    Returns ``None`` when no recognised slot keys are present.  The A2
    interpreter accepts the hint through :func:`interpret_directive`'s
    ``scope_hint`` argument and applies it to drafts the directive itself
    does not scope (unless the directive explicitly generalises).

    Explicit ``scope`` / ``scope_ref`` keys on ``slot_context`` are
    respected as-is, letting callers bypass the scope-inference heuristic
    when the UI already knows exactly which scope to apply.
    """
    if not slot_context:
        return None
    if not isinstance(slot_context, Mapping):
        raise ValueError(
            f"slot_context must be a mapping, got {type(slot_context).__name__}"
        )
    explicit_scope = slot_context.get("scope")
    if isinstance(explicit_scope, str) and explicit_scope.strip():
        scope_ref = slot_context.get("scope_ref")
        if scope_ref is not None and not isinstance(scope_ref, str):
            raise ValueError(
                f"slot_context.scope_ref must be string or null, "
                f"got {type(scope_ref).__name__}"
            )
        return {"scope": explicit_scope.strip(), "scope_ref": scope_ref}

    for key, scope in _SLOT_SCOPE_KEYS:
        if key not in slot_context:
            continue
        value = slot_context[key]
        if value is None:
            continue
        if isinstance(value, int):
            # scene_num=3 -> scope_ref="scene-3". Matches the A2
            # heuristic that extracts "scene-N" tokens from directives.
            ref = f"scene-{value}" if scope == "scene" else str(value)
        else:
            ref = str(value).strip()
            if not ref:
                continue
            if scope == "scene" and ref.isdigit():
                ref = f"scene-{int(ref)}"
        return {"scope": scope, "scope_ref": ref}
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def _safe_json_body(request: Request) -> dict[str, Any]:
    """Parse the request JSON body, returning ``{}`` on empty/invalid input.

    Accepts empty bodies (the halt button has no payload) and treats
    non-object JSON as a hard 400 at the caller.
    """
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"body is not valid JSON: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"body must be a JSON object, got {type(data).__name__}"
        )
    return data


@router.post("/halt")
async def halt_pipeline(request: Request):
    """Engage the halt flag so the pipeline pauses at the next checkpoint.

    Body (optional): ``{"reviewer": str, "reason": str}``. Returns
    immediately with the halt state.  The actual pause is effected by
    :func:`callbacks.approval_gate.wait_for_approval` which polls
    :func:`is_halt_requested` on every tick.
    """
    try:
        body = await _safe_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    reviewer = body.get("reviewer") or "l4-dashboard"
    reason = body.get("reason")
    state = set_halt_requested(reviewer=reviewer, reason=reason)
    logger.info(
        "Halt requested by %s (reason=%s) -- pipeline will pause at next "
        "safe checkpoint",
        reviewer,
        reason,
    )
    return JSONResponse({"status": "halt_requested", **state})


@router.post("/halt/release")
async def release_halt():
    """Clear the halt flag so the approval-gate loop resumes."""
    state = clear_halt()
    logger.info("Halt released -- pipeline may resume at next checkpoint")
    return JSONResponse({"status": "released", **state})


@router.get("/halt_state")
async def get_halt_state_endpoint():
    """Return the current halt state for the dashboard shell."""
    return JSONResponse(_read_halt_state())


def _dispatch_drift(state: MutableMapping[str, Any]) -> list[DriftHandlingReceipt]:
    """Hand any queued drift signals to the A6 re-manifestation executor."""
    if not pending_drift_signals(state):
        return []
    return handle_drift(state)


def _receipt_summary(receipt: DriftHandlingReceipt) -> dict[str, Any]:
    plan = receipt.plan
    plan_id = (
        f"plan-{plan.stage_name}-{plan.from_rev}-{plan.to_rev}"
        if plan is not None
        else None
    )
    return {
        "plan_id": plan_id,
        "stage_name": plan.stage_name if plan is not None else None,
        "from_rev": plan.from_rev if plan is not None else None,
        "to_rev": plan.to_rev if plan is not None else None,
        "step_count": len(receipt.step_receipts),
        "error": receipt.error,
    }


@router.post("/directive")
async def submit_directive(request: Request):
    """Parse a free-form directive through A2 and land it in the ledger.

    Body: ``{"directive": str, "slot_context": dict | null,
    "reviewer": str | null, "l4_event_id": str | null}``.

    On success returns ``{"record_ids": [...], "records": [...],
    "re_manifestation_plans": [...]}``.  On A2 parse failure returns
    HTTP 422 with the :class:`InterpreterError` message.  On ledger /
    consistency-check failure returns HTTP 500 -- these indicate a
    pipeline-invariant violation, not a reviewer mistake.
    """
    try:
        body = await _safe_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    directive = body.get("directive")
    if not isinstance(directive, str) or not directive.strip():
        return JSONResponse(
            {"error": "'directive' must be a non-empty string"},
            status_code=400,
        )
    slot_context = body.get("slot_context")
    reviewer = body.get("reviewer") or "l4-dashboard"
    l4_event_id = body.get("l4_event_id") or f"l4-{uuid.uuid4().hex[:12]}"

    try:
        scope_hint = _slot_context_to_scope_hint(slot_context)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    with _state_lock:
        state = _load_blackboard()
        try:
            records = interpret_directive(
                directive.strip(),
                reviewer=str(reviewer),
                l4_event_id=str(l4_event_id),
                scope_hint=scope_hint,
                state=state,
            )
        except InterpreterError as exc:
            logger.warning(
                "Preference Interpreter rejected directive (reviewer=%s, "
                "event=%s): %s",
                reviewer,
                l4_event_id,
                exc,
            )
            return JSONResponse(
                {
                    "error": str(exc),
                    "kind": "interpreter_parse_failure",
                    "l4_event_id": l4_event_id,
                },
                status_code=422,
            )
        except (ValueError, TypeError) as exc:
            # Malformed reviewer / event id surface here; still a 400.
            return JSONResponse(
                {"error": str(exc), "l4_event_id": l4_event_id},
                status_code=400,
            )

        try:
            # A5 + B3: detect drift against whatever stage derivations
            # exist on the shared blackboard, then hand every queued
            # signal to the re-manifestation executor.  Any exception
            # here is a wiring bug per task invariant 4.
            check_consistency_at_gate(state, stage_name="dashboard")
            receipts = _dispatch_drift(state)
        except Exception as exc:  # noqa: BLE001 -- surfaced as 500
            logger.exception(
                "A5 consistency check / A6 dispatch failed for directive "
                "(reviewer=%s, event=%s)",
                reviewer,
                l4_event_id,
            )
            return JSONResponse(
                {
                    "error": str(exc),
                    "kind": "consistency_check_failure",
                    "l4_event_id": l4_event_id,
                },
                status_code=500,
            )

        try:
            _save_blackboard(state)
        except OSError as exc:
            logger.exception("failed to persist dashboard blackboard")
            return JSONResponse(
                {
                    "error": f"failed to persist ledger state: {exc}",
                    "kind": "ledger_persistence_failure",
                    "l4_event_id": l4_event_id,
                },
                status_code=500,
            )

    record_payload = [r.to_dict() for r in records]
    plan_payload = [_receipt_summary(r) for r in receipts]
    logger.info(
        "Directive accepted: reviewer=%s event=%s records=%d plans=%d",
        reviewer,
        l4_event_id,
        len(records),
        len(plan_payload),
    )
    return JSONResponse(
        {
            "status": "accepted",
            "l4_event_id": l4_event_id,
            "record_ids": [r.revision for r in records],
            "records": record_payload,
            "re_manifestation_plans": plan_payload,
            "scope_hint": scope_hint,
        }
    )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    """Wipe disk-backed state.  Used by the pytest fixtures in
    ``server/tests/test_dashboard_directives.py``.
    """
    for path in (_HALT_FILE, _BLACKBOARD_FILE):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


__all__ = [
    "router",
    "is_halt_requested",
    "set_halt_requested",
    "clear_halt",
    "mark_halted_at_stage",
    "_read_halt_state",
    "_load_blackboard",
    "_save_blackboard",
    "_slot_context_to_scope_hint",
    "_reset_for_tests",
]
