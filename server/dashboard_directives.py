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

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Iterator, Mapping, MutableMapping, Optional

try:  # pragma: no cover -- Windows has no fcntl; fall back to a no-op.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

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


def _emit(event_type: str, data: dict) -> None:
    """Emit a pipeline event on the shared AG-UI bus.

    Wrapped in a try/except so a broken subscriber never sinks a
    directive or halt endpoint -- the contract is "best-effort
    notification", not "blocking hand-off".  Import is deferred to
    avoid a hard dependency cycle with :mod:`agui`, which pulls in a
    lot of heavy optional modules.
    """
    try:
        from agui import emit_agui_event  # local import, cycle-safe
    except Exception:  # noqa: BLE001 -- optional subsystem, never fatal
        logger.debug("emit_agui_event unavailable; skipping %s", event_type)
        return
    try:
        emit_agui_event(event_type, data)
    except Exception:  # noqa: BLE001
        logger.exception("failed to emit %s event", event_type)

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
    "halt_last_checkpoint": None,
    "halt_exit_requested": False,
}

#: Valid modes accepted by :func:`release_halt`.  ``resume`` is the
#: legacy shape -- clear the flag and return.  ``rewind`` tells the
#: pipeline to fall back to the last safe checkpoint (implemented as a
#: synthetic directive appended to the ledger so A5/A6 drift the right
#: stages).  ``exit`` flips the sticky ``halt_exit_requested`` flag so
#: the pipeline shuts down cleanly at its next safe checkpoint instead
#: of resuming.
_HALT_RELEASE_MODES: tuple[str, ...] = ("resume", "rewind", "exit")

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


@contextlib.contextmanager
def _file_lock(path: str) -> Iterator[None]:
    """Cross-process exclusive lock tied to ``path``.

    Used to serialise the read-modify-write cycles on the halt-state file
    across the server process (which owns ``/api/halt*`` endpoints) and
    the pipeline process (which calls :func:`mark_halted_at_stage` from
    the approval-gate poll loop).

    Falls back to a process-local :class:`threading.Lock` when ``fcntl``
    is unavailable (Windows, mock environments); on POSIX the lockfile
    lives next to the protected file with a ``.lock`` suffix.
    """
    if fcntl is None:  # pragma: no cover -- exercised on Windows only
        with _state_lock:
            yield
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_path = f"{path}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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
    last_checkpoint: Optional[str] = None,
) -> dict[str, Any]:
    """Engage the halt flag.  Returns the new halt state."""
    with _file_lock(_HALT_FILE):
        state = _read_halt_state()
        state["halt_requested"] = True
        if reviewer is not None:
            state["halt_reviewer"] = reviewer
        if reason is not None:
            state["halt_reason"] = reason
        if at_stage is not None:
            state["halted_at_stage"] = at_stage
        if last_checkpoint is not None:
            state["halt_last_checkpoint"] = last_checkpoint
        state["halt_timestamp"] = time.time()
        _write_halt_state(state)
    return state


def clear_halt(*, preserve_exit_flag: bool = False) -> dict[str, Any]:
    """Release the halt flag so the gate-poll loop can resume.

    ``preserve_exit_flag`` keeps ``halt_exit_requested`` set in the
    returned state -- used by the ``exit`` release mode so the
    approval-gate loop still observes the terminate-after-checkpoint
    request even after the halt flag itself clears.
    """
    with _file_lock(_HALT_FILE):
        state = dict(_HALT_DEFAULT)
        if preserve_exit_flag:
            state["halt_exit_requested"] = True
        _write_halt_state(state)
    return state


def mark_halted_at_stage(stage_name: str, *, last_checkpoint: Optional[str] = None) -> None:
    """Record the stage the pipeline paused at after observing the flag.

    Called by the approval-gate poll loop the first time it observes the
    halt flag. No-op when the flag is not set.

    Guarded by :func:`_file_lock` so the read-modify-write cycle cannot
    race with :func:`clear_halt` in the server process -- otherwise a
    mid-release gate-poll tick could write back a stale ``halt_requested:
    True`` and silently re-engage the halt the reviewer just released.
    """
    if not stage_name:
        return
    with _file_lock(_HALT_FILE):
        state = _read_halt_state()
        if not state.get("halt_requested"):
            return
        if (
            state.get("halted_at_stage") == stage_name
            and (last_checkpoint is None
                 or state.get("halt_last_checkpoint") == last_checkpoint)
        ):
            return
        state["halted_at_stage"] = stage_name
        if last_checkpoint is not None:
            state["halt_last_checkpoint"] = last_checkpoint
        state["halt_timestamp"] = time.time()
        _write_halt_state(state)
    # Tell the dashboard the pipeline actually paused here -- this is
    # the event the "paused at X" banner subscribes to. Emitting outside
    # the lock avoids blocking the gate-poll loop on slow subscribers.
    _emit("halt_fired", {
        "stage": stage_name,
        "last_checkpoint": last_checkpoint,
        "reviewer": state.get("halt_reviewer"),
        "reason": state.get("halt_reason"),
    })


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
    last_checkpoint = body.get("last_checkpoint")
    if last_checkpoint is not None and not isinstance(last_checkpoint, str):
        return JSONResponse(
            {"error": "last_checkpoint must be a string"}, status_code=400
        )
    state = set_halt_requested(
        reviewer=reviewer,
        reason=reason,
        last_checkpoint=last_checkpoint,
    )
    logger.info(
        "Halt requested by %s (reason=%s) -- pipeline will pause at next "
        "safe checkpoint",
        reviewer,
        reason,
    )
    # Surface the halt on the chat + timeline immediately.  The gate-poll
    # loop will re-emit ``halt_fired`` with the actual stage it paused
    # at via :func:`mark_halted_at_stage`; this first event tells the
    # dashboard "the button was pressed, expect a pause soon".
    _emit("halt_fired", {
        "stage": state.get("halted_at_stage"),
        "last_checkpoint": state.get("halt_last_checkpoint"),
        "reviewer": reviewer,
        "reason": reason,
        "phase": "requested",
    })
    # UI-01 (#186): narrator chat turn announcing the halt.
    try:
        from agents.chat_narrator import emit_narrator_event  # type: ignore
        emit_narrator_event(
            "halt_fired",
            fields={
                "stage": state.get("halted_at_stage") or "pipeline",
                "checkpoint": state.get("halt_last_checkpoint") or "next safe checkpoint",
                "reason": reason or "",
                "reviewer": reviewer,
            },
        )
    except Exception as exc:  # noqa: BLE001 -- best-effort UI hook
        logger.debug("chat_narrator halt_fired emission failed: %s", exc)
    return JSONResponse({"status": "halt_requested", **state})


def _append_rewind_directive(checkpoint: Optional[str]) -> None:
    """Inject a synthetic directive that rewinds to the last checkpoint.

    The rewind is modelled as a regular preference-ledger record so A5
    observes it on the next consistency check and A6 re-manifests the
    right slots.  Content is deliberately conservative: no fixed vocab
    so the interpreter's closed-vocab heuristics do not reject it, and
    the scope is GLOBAL so every stage downstream of the checkpoint
    gets revisited.
    """
    import datetime as _dt

    from callbacks.preference_ledger import (  # cycle-safe local import
        Origin,
        Polarity,
        Scope,
        Subject,
        append_preference,
    )

    ref = (checkpoint or "last-safe-checkpoint").strip() or "last-safe-checkpoint"
    origin = Origin(
        l4_event_id=f"rewind-{uuid.uuid4().hex[:8]}",
        reviewer="l4-dashboard-rewind",
        timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(),
    )
    with _state_lock:
        state = _load_blackboard()
        append_preference(
            state,
            scope=Scope.GLOBAL,
            polarity=Polarity.PREFER,
            subject=Subject.NARRATIVE_STRUCTURE,
            content=f"rewind to {ref}",
            origin=origin,
            metadata={"rewind_checkpoint": ref, "kind": "halt_rewind"},
        )
        _save_blackboard(state)


@router.post("/halt/release")
async def release_halt(request: Request):
    """Clear the halt flag so the approval-gate loop resumes.

    Body (optional): ``{"mode": "resume" | "rewind" | "exit"}``.  The
    default is ``resume`` (legacy shape).  ``rewind`` additionally
    appends a synthetic directive to the ledger so A5/A6 roll the
    pipeline back to ``halt_last_checkpoint``.  ``exit`` sets a sticky
    exit flag that the approval-gate loop reads to terminate the run.
    """
    try:
        body = await _safe_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    mode = body.get("mode", "resume")
    if not isinstance(mode, str) or mode not in _HALT_RELEASE_MODES:
        return JSONResponse(
            {
                "error": (
                    f"mode must be one of {_HALT_RELEASE_MODES}, "
                    f"got {mode!r}"
                ),
            },
            status_code=400,
        )

    prior = _read_halt_state()
    checkpoint = prior.get("halt_last_checkpoint") or prior.get("halted_at_stage")

    if mode == "rewind":
        # _append_rewind_directive acquires the long-held ``_state_lock``
        # that ``_run_directive_sync`` can hold for seconds while A5/A6
        # run.  Offload to a worker so the event loop (and the halt
        # button) stay responsive per ARCH-H4.
        try:
            await asyncio.to_thread(_append_rewind_directive, checkpoint)
        except Exception:  # noqa: BLE001 -- surface but still release
            logger.exception("rewind directive failed to append; releasing anyway")

    state = clear_halt(preserve_exit_flag=(mode == "exit"))
    logger.info(
        "Halt released (mode=%s, checkpoint=%s) -- pipeline may resume",
        mode,
        checkpoint,
    )
    _emit("halt_released", {
        "mode": mode,
        "last_checkpoint": checkpoint,
        "stage": prior.get("halted_at_stage"),
    })
    return JSONResponse({"status": "released", "mode": mode, **state})


@router.get("/halt_state")
async def get_halt_state_endpoint():
    """Return the current halt state for the dashboard shell."""
    return JSONResponse(_read_halt_state())


def _dispatch_drift(state: MutableMapping[str, Any]) -> list[DriftHandlingReceipt]:
    """Hand any queued drift signals to the A6 re-manifestation executor."""
    if not pending_drift_signals(state):
        return []
    return handle_drift(state)


def _int_suffix(token: Optional[str], prefix: str) -> Optional[int]:
    """Parse the integer suffix of a ``{prefix}N`` token."""
    if not token:
        return None
    if token.startswith(prefix):
        tail = token[len(prefix):]
        if tail.isdigit():
            return int(tail)
    # Fallback: take trailing digits after the last hyphen.
    idx = token.rfind("-")
    if idx >= 0:
        tail = token[idx + 1:]
        if tail.isdigit():
            return int(tail)
    return None


def _derive_slot_ids(
    *,
    scene_id: Optional[str],
    clip_id: Optional[str],
    artifact_key: Optional[str],
) -> list[str]:
    """Map a plan step's scene/clip/artifact_key to OTIO slot ids.

    OTIO slot ids are ``{track_prefix}:{scene_num}:{phrase_idx}`` --
    see :func:`agui._emit_slot_state_from_artifact`.  A single step
    conceptually touches every track at the ``(scene, phrase)``
    coordinate, so we fan out across the three canonical tracks.

    Returns an empty list when no parseable scene token is present --
    scene-less drifts surface only as ``drifted_scene_nums`` or as the
    raw ``artifact_key`` in the event payload.
    """
    scene_num = _int_suffix(scene_id, "scene-")
    if scene_num is None and artifact_key:
        scene_num = _int_suffix(_extract_token(artifact_key, "scene-"), "scene-")
    if scene_num is None:
        return []

    phrase_idx = _int_suffix(clip_id, "clip-")
    if phrase_idx is None and artifact_key:
        phrase_idx = _int_suffix(_extract_token(artifact_key, "clip-"), "clip-")

    if phrase_idx is None:
        # Scene-wide drift: no specific phrase.  Callers use
        # ``drifted_scene_nums`` to light up every phrase in the scene.
        return []

    return [
        f"V1:{scene_num}:{phrase_idx}",
        f"A1:{scene_num}:{phrase_idx}",
        f"A2:{scene_num}:{phrase_idx}",
    ]


def _extract_token(haystack: str, prefix: str) -> Optional[str]:
    """Return ``{prefix}N`` from ``haystack`` if present, else ``None``."""
    idx = haystack.find(prefix)
    if idx < 0:
        return None
    tail = haystack[idx + len(prefix):]
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return None
    return f"{prefix}{digits}"


def _drifted_slot_summary(
    receipts: list[DriftHandlingReceipt],
) -> tuple[list[str], list[int], list[dict[str, Any]]]:
    """Walk re-manifestation receipts to build the drift payload.

    Returns:
        (slot_ids, scene_nums, step_descriptors)
        - ``slot_ids``: deterministic-sorted list of OTIO slot ids.
        - ``scene_nums``: scenes whose exact phrase index was unknown.
        - ``step_descriptors``: one dict per plan step with scene/clip/
          slot_ids/status, used to emit per-step progress events.
    """
    slot_ids: list[str] = []
    slot_seen: set[str] = set()
    scene_nums: list[int] = []
    scene_seen: set[int] = set()
    step_descriptors: list[dict[str, Any]] = []

    for receipt in receipts:
        plan = receipt.plan
        step_receipts = list(receipt.step_receipts)
        for i, step in enumerate(plan.steps):
            ids = _derive_slot_ids(
                scene_id=step.scene_id,
                clip_id=step.clip_id,
                artifact_key=step.artifact_key,
            )
            for sid in ids:
                if sid not in slot_seen:
                    slot_seen.add(sid)
                    slot_ids.append(sid)
            scene_num = _int_suffix(step.scene_id, "scene-")
            if scene_num is None and step.artifact_key:
                scene_num = _int_suffix(
                    _extract_token(step.artifact_key, "scene-"), "scene-"
                )
            # Only mark scene-wide drift when we could not derive any
            # per-slot triples.  If we have specific slot_ids the UI will
            # paint those individually; adding the scene too would leave
            # orphan slots (same scene, different phrase) stuck on the
            # amber outline because their teardown only fires for the
            # scene-wide case.
            if (
                scene_num is not None
                and not ids
                and scene_num not in scene_seen
            ):
                scene_seen.add(scene_num)
                scene_nums.append(scene_num)

            status = "queued"
            error: Optional[str] = None
            if receipt.error:
                status = "failed"
                error = receipt.error
            elif i < len(step_receipts):
                sr = step_receipts[i]
                st = sr.get("status") if isinstance(sr, Mapping) else None
                if st in ("dispatched", "queued"):
                    status = "queued"
                elif st == "failed":
                    status = "failed"
                    error = (
                        sr.get("error") if isinstance(sr, Mapping) else None
                    )
                elif st:
                    status = str(st)

            step_descriptors.append({
                "plan_id": f"plan-{plan.stage_name}-{plan.from_rev}-{plan.to_rev}",
                "stage_name": plan.stage_name,
                "action": step.action,
                "artifact_key": step.artifact_key,
                "scene_id": step.scene_id,
                "clip_id": step.clip_id,
                "scene_num": scene_num,
                "slot_ids": ids,
                "reason": step.reason,
                "status": status,
                "error": error,
            })

    slot_ids.sort()
    scene_nums.sort()
    return slot_ids, scene_nums, step_descriptors


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


def _run_directive_sync(
    *,
    directive: str,
    reviewer: str,
    l4_event_id: str,
    scope_hint: Optional[dict[str, Any]],
) -> JSONResponse:
    """Synchronous core of :func:`submit_directive`.

    Pulled out so the async endpoint can offload it onto a thread-pool
    worker via :func:`asyncio.to_thread`.  The Preference Interpreter makes
    a blocking HTTP call to the LLM provider and the A6 re-manifestation
    executor can spend multi-second stretches inside ``handle_drift``;
    running the whole locked cycle on the event loop would freeze every
    other endpoint -- including the halt button, which must stay
    responsive for the whole point of H4 to hold.
    """
    with _state_lock:
        state = _load_blackboard()
        try:
            records = interpret_directive(
                directive,
                reviewer=reviewer,
                l4_event_id=l4_event_id,
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

    # UI-03c (#200): a stage-scoped directive is a "reject with note" on
    # an inline approval gate.  The ledger record has already been
    # appended above; now flip the approval flag so the wait_for_approval
    # poll loop exits and the pipeline moves on with the new directive
    # applied as drift on downstream stages.  Outside the _state_lock:
    # the approval state file is owned by callbacks.approval_gate and
    # uses its own on-disk write.
    released_stage: Optional[str] = None
    if scope_hint is not None and scope_hint.get("scope") == "stage":
        stage_ref = scope_hint.get("scope_ref")
        if isinstance(stage_ref, str) and stage_ref.strip():
            released_stage = stage_ref.strip()
            try:
                from callbacks.approval_gate import approve_stage

                approve_stage(released_stage)
                logger.info(
                    "Stage-scoped directive released approval gate "
                    "(stage=%s, reviewer=%s, event=%s)",
                    released_stage,
                    reviewer,
                    l4_event_id,
                )
            except Exception as exc:  # pragma: no cover -- defensive
                logger.warning(
                    "Stage-scoped directive failed to release gate "
                    "(stage=%s): %s",
                    released_stage,
                    exc,
                )

    record_payload = [r.to_dict() for r in records]
    plan_payload = [_receipt_summary(r) for r in receipts]
    drifted_slot_ids, drifted_scene_nums, step_descriptors = (
        _drifted_slot_summary(receipts)
    )
    record_ids = [r.revision for r in records]
    logger.info(
        "Directive accepted: reviewer=%s event=%s records=%d plans=%d slots=%d",
        reviewer,
        l4_event_id,
        len(records),
        len(plan_payload),
        len(drifted_slot_ids),
    )

    # UI-05a: emit the directive_applied + per-step re_manifestation_progress
    # events onto the shared AG-UI bus so the dashboard can echo the
    # directive, light up drifted slots, and drip the progress badge
    # back as each step finishes.
    _emit("directive_applied", {
        "directive_text": directive,
        "l4_event_id": l4_event_id,
        "reviewer": reviewer,
        "ledger_record_ids": record_ids,
        "records": record_payload,
        "drifted_slot_ids": drifted_slot_ids,
        "drifted_scene_nums": drifted_scene_nums,
        "scope": scope_hint,
        "re_manifestation_plans": plan_payload,
    })
    for desc in step_descriptors:
        _emit("re_manifestation_progress", {
            **desc,
            "phase": "start",
        })
        terminal_phase = "failed" if desc["status"] == "failed" else "complete"
        _emit("re_manifestation_progress", {
            **desc,
            "phase": terminal_phase,
        })

    # UI-01 (#186): narrator chat turn. ``n_drifted`` is the count of
    # re-manifestation plans actually scheduled.
    try:
        from agents.chat_narrator import emit_narrator_event  # type: ignore
        emit_narrator_event(
            "directive_applied",
            fields={
                "directive_text": directive,
                "n_drifted": len(plan_payload),
                "reviewer": reviewer,
            },
        )
    except Exception as exc:  # noqa: BLE001 -- best-effort UI hook
        logger.debug("chat_narrator directive_applied emission failed: %s", exc)

    return JSONResponse(
        {
            "status": "accepted",
            "l4_event_id": l4_event_id,
            "record_ids": record_ids,
            "records": record_payload,
            "re_manifestation_plans": plan_payload,
            "drifted_slot_ids": drifted_slot_ids,
            "drifted_scene_nums": drifted_scene_nums,
            "scope_hint": scope_hint,
            "released_stage": released_stage,
        }
    )


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

    The heavy lifting (LLM call, file I/O, A5 + A6) is dispatched to a
    thread-pool worker via :func:`asyncio.to_thread` so the asyncio event
    loop stays responsive -- critically, the halt button endpoint keeps
    answering while a directive is being parsed.
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

    return await asyncio.to_thread(
        _run_directive_sync,
        directive=directive.strip(),
        reviewer=str(reviewer),
        l4_event_id=str(l4_event_id),
        scope_hint=scope_hint,
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
