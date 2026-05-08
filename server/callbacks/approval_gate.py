"""
Approval Gate — human-in-the-loop checkpoints between pipeline stages.

The pipeline pauses after each stage completes, waits for human approval
via the dashboard, then proceeds to the next stage.

Approval state is persisted to disk so it survives restarts.

Flow:
    Stage completes → after_agent_callback signals "ready for review"
    Dashboard shows data + "Approve" button
    Human clicks "Approve" → POST /agui/approve
    Next stage's before_agent_callback polls until approved → proceeds
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, MutableMapping, Optional

from google.genai import types as genai_types

logger = logging.getLogger(__name__)

_OUTPUT_DIR = os.environ.get("PIPELINE_OUTPUT_DIR", "/tmp/documentary-pipeline")
_APPROVAL_FILE = os.path.join(_OUTPUT_DIR, ".approval_state.json")

# Auto-approve all stages (no human needed) when DOCUMENTARY_AUTO_APPROVE is
# set. Used by unattended production runs (e.g. scheduled jobs) where no
# human reviewer is online to click the approval card. Honoured in lockstep
# with gatekeeper.py and recovery.py, which read the same env var to bypass
# their own intervention windows / L4 escalation paths.
_AUTO_APPROVE_ENV = os.environ.get(
    "DOCUMENTARY_AUTO_APPROVE", ""
).strip().lower() in ("1", "true", "yes")

# How often to poll for approval (seconds)
_POLL_INTERVAL = 5.0

# Maximum time to wait for approval before timing out (seconds)
# 2 hours — generous, human may step away
_MAX_WAIT = 7200.0


def _read_approval_state() -> dict:
    """Read approval state from disk."""
    if not os.path.exists(_APPROVAL_FILE):
        return {}
    try:
        with open(_APPROVAL_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_approval_state(state: dict) -> None:
    """Write approval state to disk."""
    os.makedirs(os.path.dirname(_APPROVAL_FILE), exist_ok=True)
    with open(_APPROVAL_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_stage_approved(stage: str) -> bool:
    """Check if a stage has been approved by the human.

    When ``DOCUMENTARY_AUTO_APPROVE`` is set, every stage is treated as
    approved so unattended runs do not block on the human checkpoint.
    """
    if _AUTO_APPROVE_ENV:
        return True
    state = _read_approval_state()
    return state.get(stage, {}).get("approved", False)


def reset_stage_approval(stage: str) -> None:
    """Clear the approval for ``stage`` so the next :func:`wait_for_approval`
    actually blocks for human review.

    Required by ARCH-B3 (#139) reconstruction gating: a single pipeline run
    may trigger multiple reconstruction plans (e.g. drift at the scenario
    boundary, then drift at the audio boundary). Without resetting, the
    second and later plans would short-circuit through
    :func:`is_stage_approved` because the ``approved`` flag persists on
    disk from the first approval, silently auto-approving every subsequent
    reconstruction.
    """
    state = _read_approval_state()
    if stage in state:
        state[stage].pop("approved", None)
        state[stage].pop("approved_at", None)
        state[stage].pop("approved_by", None)
        state[stage].pop("ready", None)
        state[stage].pop("ready_at", None)
        _write_approval_state(state)
        logger.info("Stage '%s' approval reset for re-gating", stage)


def mark_stage_ready(stage: str) -> None:
    """Mark a stage as ready for human review (but not yet approved)."""
    state = _read_approval_state()
    if stage not in state:
        state[stage] = {}
    state[stage]["ready"] = True
    state[stage]["ready_at"] = time.time()
    _write_approval_state(state)
    logger.info("Stage '%s' marked ready for review", stage)
    _emit_gate_digest("gate_open", stage)


def approve_stage(stage: str, *, reviewer: str = "programmatic") -> None:
    """Programmatically approve a stage (no human needed).

    Used by automated paths that need downstream stages to proceed
    without a human reviewer.
    """
    state = _read_approval_state()
    if stage not in state:
        state[stage] = {}
    state[stage]["ready"] = True
    state[stage]["approved"] = True
    state[stage]["approved_at"] = time.time()
    state[stage]["approved_by"] = reviewer
    _write_approval_state(state)
    logger.info("Stage '%s' programmatically approved (%s)", stage, reviewer)
    _emit_gate_digest(
        "gate_close", stage, decision="approved", reviewer=reviewer
    )


def _emit_gate_digest(
    kind: str,
    stage: str,
    *,
    decision: str = "",
    reviewer: str = "",
) -> None:
    """Fire-and-forget emission of a gate-open / gate-close reasoning digest.

    ARCH-H5 (issue #160) wiring. Never raise -- the gate module must not
    regress just because the digest bus is unavailable.
    """
    try:
        from dashboard.reasoning_digest import emit_digest

        event: dict[str, Any] = {"stage": stage}
        if decision:
            event["decision"] = decision
        if reviewer:
            event["reviewer"] = reviewer
        emit_digest(None, kind, event)
    except Exception as exc:  # pragma: no cover -- defensive
        logger.debug("reasoning_digest %s emission failed: %s", kind, exc)


def _emit_approval_gate_event(
    event_type: str,
    stage: str,
    *,
    decision: str = "",
    reviewer: str = "",
    boundary_slot_id: str = "",
) -> None:
    """UI-03a (#198): emit approval_gate_opened / approval_gate_closed on
    the unified AG-UI pipeline event bus.

    These events land on the single SSE connection alongside agent turns
    and drive the inline approval card on the OTIO timeline (UI-03b,
    #199) plus the narrator chat surface (UI-01).  Fire-and-forget: never
    raise -- the gate module must not regress just because the event bus
    is unavailable.
    """
    try:
        # Local import so the callbacks package can be imported when the
        # FastAPI surface is not loaded (e.g. unit tests that exercise
        # only the pipeline).
        from agui import emit_agui_event

        payload: dict[str, Any] = {"stage": stage}
        if event_type == "approval_gate_opened":
            payload["opened_at"] = time.time()
        else:
            payload["closed_at"] = time.time()
        if decision:
            payload["decision"] = decision
        if reviewer:
            payload["reviewer"] = reviewer
        if boundary_slot_id:
            payload["boundary_slot_id"] = boundary_slot_id
        emit_agui_event(event_type, payload)
    except Exception as exc:  # pragma: no cover -- defensive
        logger.debug("emit_agui_event %s emission failed: %s", event_type, exc)


def wait_for_approval(
    stage: str,
    *,
    state: Optional[MutableMapping[str, Any]] = None,
) -> bool:
    """Block until the human approves the given stage.

    Returns True if approved, False if timed out.

    Args:
        stage: The stage name being gated.
        state: Optional ADK session state. When provided, ARCH-B2 (issue
            #138) runs a consistency check against the Preference Ledger
            every poll interval so drift observed *during* human review
            is dispatched to ARCH-B3 (#139) immediately, rather than
            waiting for the next stage boundary. When ``None`` the poll
            loop skips the drift check (preserves the pre-B2 behaviour
            for callers that lack a handle on ``state``).
    """
    start = time.time()
    logger.info("Waiting for human approval of stage '%s'...", stage)

    # UI-03a (#198): announce the gate exactly once per entry so the
    # inline approval card (UI-03b, #199) can render and the narrator
    # (UI-01) can surface "Stage ready -- approve or reject" in chat.
    # The per-poll-tick check is INSIDE the loop below; this emission
    # must stay outside it.
    _emit_approval_gate_event("approval_gate_opened", stage)
    _decision = "timeout"

    # Local import: consistency_gate imports approval_gate at module level
    # (for reconstruction gating), so importing it at top-level would form
    # a circular import. Deferring it to first poll breaks the cycle.
    _gate_poll_check = None
    if state is not None:
        try:
            from callbacks.consistency_gate import gate_poll_consistency_check

            _gate_poll_check = gate_poll_consistency_check
        except Exception as exc:  # pragma: no cover -- defensive
            logger.warning(
                "wait_for_approval: consistency_gate unavailable (%s); "
                "skipping per-poll drift check for stage %r",
                exc,
                stage,
            )

    # ARCH-H4 (#159): the halt-anywhere button sets a disk-backed flag
    # that the dashboard polls on every tick.  When engaged, the
    # approval-gate loop stays blocked even if the stage was marked
    # approved -- the pipeline pauses at the NEXT safe checkpoint rather
    # than aborting a mid-stage tool call.  Clearing the flag lets the
    # next poll tick fall through to the normal approval check.
    _halt_probe = None
    _halt_marker = None
    try:
        from dashboard_directives import (
            is_halt_requested as _halt_probe,
            mark_halted_at_stage as _halt_marker,
        )
    except Exception as exc:  # pragma: no cover -- defensive
        logger.warning(
            "wait_for_approval: dashboard_directives unavailable (%s); "
            "halt-anywhere flag will not be observed for stage %r",
            exc,
            stage,
        )

    try:
        while time.time() - start < _MAX_WAIT:
            if _halt_probe is not None and _halt_probe():
                # Record the stage we paused at (idempotent after first call)
                # so the dashboard can surface which checkpoint the pipeline
                # is currently waiting on.  Do not return -- stay blocked
                # until the halt flag is released, even if the stage is
                # otherwise approved.
                if _halt_marker is not None:
                    try:
                        _halt_marker(stage)
                    except Exception as exc:  # pragma: no cover -- defensive
                        logger.warning(
                            "wait_for_approval: failed to mark halt stage "
                            "%r: %s",
                            stage,
                            exc,
                        )
                time.sleep(_POLL_INTERVAL)
                continue
            if is_stage_approved(stage):
                elapsed = time.time() - start
                logger.info("Stage '%s' approved after %.1fs", stage, elapsed)
                _decision = "approved"
                return True
            # ARCH-B2: run A5 consistency check every poll so drift that
            # appears while humans review is caught and dispatched to B3.
            if _gate_poll_check is not None and state is not None:
                try:
                    _gate_poll_check(state, stage)
                except RuntimeError:
                    # Invariant violation (missing ledger, rev decrease) --
                    # propagate so the pipeline stops loud, not silent.
                    _decision = "error"
                    raise
                except Exception as exc:  # pragma: no cover -- defensive
                    logger.warning(
                        "wait_for_approval: gate-poll consistency check raised "
                        "non-invariant error for stage %r: %s",
                        stage,
                        exc,
                    )
            time.sleep(_POLL_INTERVAL)

        logger.warning(
            "Timed out waiting for approval of stage '%s' (%.0fs)",
            stage,
            _MAX_WAIT,
        )
        return False
    finally:
        # UI-03a (#198): parallel close event so the inline approval card
        # (UI-03b, #199) can tear down and the narrator can announce the
        # outcome.  Emitted regardless of whether the flag flipped via
        # /agui/approve or via a stage-scoped directive (UI-03c, #200).
        _emit_approval_gate_event(
            "approval_gate_closed", stage, decision=_decision
        )


# ---------------------------------------------------------------------------
# Callback factories — create before/after callbacks for each stage
# ---------------------------------------------------------------------------

def make_after_stage_callback(stage: str):
    """Create an after_agent_callback that marks a stage ready for review.

    This wraps any existing after_agent_callback so both run.  The wrapper
    also emits a ``stage_end`` reasoning digest (ARCH-H5, issue #160) so
    the dashboard sees one plain-english line per stage boundary without
    each agent having to wire it up by hand.
    """
    def _after_callback(callback_context: Any) -> Optional[genai_types.Content]:
        mark_stage_ready(stage)
        # ARCH-H5 (issue #160): stage_end digest.  The state mapping is
        # the ADK session state so cross-stage consumers reading
        # ``reasoning_digest_log`` get a chronological record.
        try:
            from dashboard.reasoning_digest import emit_digest

            emit_digest(
                getattr(callback_context, "state", None),
                "stage_end",
                {"stage": stage, "status": "ok"},
            )
        except Exception as exc:  # pragma: no cover -- defensive
            logger.debug("reasoning_digest stage_end emission failed: %s", exc)
        return None
    return _after_callback


def make_before_stage_callback(requires_stage: str):
    """Create a before_agent_callback that waits for a prerequisite stage approval.

    The callback blocks (polls) until the required stage is approved.
    If timed out, it returns Content to skip the agent with an error.

    Note: this callback does **not** emit a ``stage_start`` reasoning
    digest. ``requires_stage`` here is the *prerequisite* stage whose
    approval gates the current agent; it is not the stage that is about
    to start.  stage_start digests should be emitted by call sites that
    actually know the identity of the stage being entered
    (see :func:`dashboard.reasoning_digest.emit_digest`).
    """
    def _before_callback(callback_context: Any) -> Optional[genai_types.Content]:
        if not is_stage_approved(requires_stage):
            logger.info(
                "Stage requires '%s' approval — waiting...", requires_stage
            )
            # ARCH-B2 (#138): forward session state so the per-poll
            # consistency check fires while humans review. Matches the
            # pattern used by every direct gate-wrapper in pipeline.py
            # and by _gate_reconstruction in remanifestation.py.
            approved = wait_for_approval(
                requires_stage, state=callback_context.state
            )
            if not approved:
                return genai_types.Content(
                    role="model",
                    parts=[genai_types.Part(
                        text=f"ERROR: Timed out waiting for '{requires_stage}' approval. "
                             f"Please approve the {requires_stage} stage on the dashboard."
                    )],
                )
        return None
    return _before_callback
