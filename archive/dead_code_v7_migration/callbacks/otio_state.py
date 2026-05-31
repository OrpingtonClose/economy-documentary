"""
OTIO state machine — ``draft`` → ``authoritative`` transition guard (ARCH-E1).

The OTIO timeline carries a formal ``state`` field on the blackboard and
(mirrored) on the timeline-file root metadata.  It starts as ``draft``
the moment the timeline is created during Stage One and crystallises to
``authoritative`` at the END of the audio stage — once narration
reconciliation has measured actual TTS durations and pacing is locked.

Once ``authoritative``, downstream stages (visual direction, production,
assembly) bind to the timeline but may NOT mutate its authoritative
baseline — narration clip boundaries and per-scene timing are the LAW.
Any attempt to mutate authoritative OTIO raises a structured
:class:`OtioStateViolation`.  The only escape hatch is an explicit
REPLACE / EXTEND escalation (ARCH-C dual-axis escalation), which opens
the guard via :func:`begin_escalation` so audio can be re-derived.

This is the timeline-level analogue of the Media Immutability Invariant
(ARCH-F) — the latter protects generated WAV/MP4 files; this protects
the OTIO structure that binds them together.

Spec references:
    - Issue #147 (ARCH-E1 Draft → Authoritative formal state transition)
    - Parent issue #127 (ARCH-E Authoritative OTIO + reconciliation + QA)
    - Meta issue #122 (ARCH-2026 architecture conformance)
    - ``docs/ARCHITECTURE_DIAGRAMS.md`` diagrams 1 + 2 (PR #121)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

OTIO_STATE_DRAFT = "draft"
OTIO_STATE_AUTHORITATIVE = "authoritative"

_VALID_STATES = frozenset({OTIO_STATE_DRAFT, OTIO_STATE_AUTHORITATIVE})

# Blackboard key for the OTIO lifecycle state.
STATE_KEY = "otio_state"

# Blackboard key for an in-flight escalation (opens the mutation guard).
# Value shape: ``{"type": "REPLACE"|"EXTEND", "reason": str, "opened_by": str}``
ESCALATION_KEY = "otio_state_escalation"

# Blackboard key recording the history of transitions (for observability).
# List of ``{"from": str, "to": str, "reason": str, "phase": str, "ts": float}``.
HISTORY_KEY = "otio_state_history"


# ---------------------------------------------------------------------------
# Structured failure
# ---------------------------------------------------------------------------


class OtioStateViolation(RuntimeError):
    """Raised when a caller tries to mutate an authoritative OTIO timeline.

    Carries a structured ``details`` dict so log/telemetry consumers can
    reason about the violation without parsing the message string.  The
    ``details`` dict always contains:

    - ``operation``: name of the mutating operation
    - ``otio_state``: current state (always ``"authoritative"`` at raise)
    - ``timeline_path``: file path of the offending timeline (best-effort)
    - ``escalation``: current escalation record, or ``None`` (means no
      explicit escalation was open — the mutation is forbidden)
    """

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


# ---------------------------------------------------------------------------
# State readers / writers
# ---------------------------------------------------------------------------


def get_otio_state(state: Any) -> str:
    """Return the current OTIO lifecycle state from the blackboard.

    Defaults to ``OTIO_STATE_DRAFT`` when no state has been set yet —
    the pipeline starts in draft and only crystallises at end of audio.
    """
    if state is None:
        return OTIO_STATE_DRAFT
    try:
        current = state.get(STATE_KEY, OTIO_STATE_DRAFT)
    except Exception:  # noqa: BLE001 — state may be a non-Mapping stub
        return OTIO_STATE_DRAFT
    if current not in _VALID_STATES:
        return OTIO_STATE_DRAFT
    return current


def _stamp_on_timeline_file(timeline_path: str, new_state: str, reason: str) -> None:
    """Mirror the state onto the timeline-file root metadata.

    Persisting the state into the OTIO file means that on pipeline
    resume (B2 checkpoint restore / re-mount) the authoritative status
    survives a process restart — the guard is not purely in-memory.

    Best-effort: logs and returns if the OTIO file is missing/unreadable.
    The blackboard state remains authoritative for in-process enforcement.
    """
    if not timeline_path or not os.path.exists(timeline_path):
        logger.debug(
            "otio_state: skipping on-disk stamp — timeline file missing: %s",
            timeline_path,
        )
        return
    try:
        import opentimelineio as otio
        from tools.otio_tools import _otio_lock

        with _otio_lock:
            timeline = otio.adapters.read_from_file(timeline_path)
            doc_meta = timeline.metadata.setdefault("documentary", {})
            doc_meta["state"] = new_state
            if reason:
                doc_meta["state_reason"] = reason
            otio.adapters.write_to_file(timeline, timeline_path)
    except Exception as exc:  # noqa: BLE001 — stamp is best-effort
        logger.warning(
            "otio_state: failed to stamp %s onto timeline file %s: %r",
            new_state, timeline_path, exc,
        )


def set_otio_state(
    state: Any,
    new_state: str,
    *,
    timeline_path: Optional[str] = None,
    reason: str = "",
    phase: str = "",
) -> None:
    """Set the OTIO lifecycle state on the blackboard and on the timeline file.

    This is the low-level setter; prefer the callback
    :func:`authoritative_transition_callback` for the end-of-audio
    transition and :func:`reset_to_draft` for REPLACE/EXTEND escalations.
    """
    if new_state not in _VALID_STATES:
        raise ValueError(
            f"Invalid OTIO state {new_state!r}; "
            f"expected one of {sorted(_VALID_STATES)}"
        )
    if state is None:
        raise ValueError("set_otio_state: state blackboard is None")

    prior = get_otio_state(state)
    state[STATE_KEY] = new_state

    history = state.get(HISTORY_KEY)
    if not isinstance(history, list):
        history = []
    history.append({
        "from": prior,
        "to": new_state,
        "reason": reason,
        "phase": phase,
    })
    state[HISTORY_KEY] = history

    tp = timeline_path or state.get("_timeline_path", "")
    if tp:
        _stamp_on_timeline_file(tp, new_state, reason)

    logger.info(
        "otio_state: %s -> %s (phase=%s, reason=%s)",
        prior, new_state, phase or "?", reason or "-",
    )

    # ARCH-H2: emit the crystallisation event onto the AG-UI bus so the
    # centrepiece dashboard can drop its reconciliation overlay without
    # polling.  Best-effort: we never want an SSE publish to destabilise
    # the OTIO state machine itself.
    if (
        new_state == OTIO_STATE_AUTHORITATIVE
        and prior != OTIO_STATE_AUTHORITATIVE
    ):
        try:
            from agui import emit_otio_authoritative  # local import; avoid cycle
            emit_otio_authoritative(timeline_path=tp, reason=reason)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "otio_state: failed to emit authoritative event: %r", exc
            )


def mark_timeline_draft(state: Any, *, timeline_path: Optional[str] = None) -> None:
    """Stamp a freshly-created timeline as ``draft``.

    Called from :func:`tools.otio_tools.create_timeline` so that every
    newly-minted timeline starts in the correct lifecycle state.
    """
    set_otio_state(
        state,
        OTIO_STATE_DRAFT,
        timeline_path=timeline_path,
        reason="timeline_created",
        phase="scenario",
    )


def reset_to_draft(state: Any, *, reason: str, timeline_path: Optional[str] = None) -> None:
    """Reset an authoritative timeline back to draft for REPLACE/EXTEND escalation.

    This is the ONLY legitimate way to revert from ``authoritative`` to
    ``draft``.  The escalation machinery (ARCH-C) calls this before
    re-running the audio stage so that narration mutations are permitted
    again; the audio stage's transition callback will crystallise it
    back to authoritative when reconciliation passes a second time.
    """
    if not reason:
        raise ValueError("reset_to_draft: 'reason' is required for audit trail")
    set_otio_state(
        state,
        OTIO_STATE_DRAFT,
        timeline_path=timeline_path,
        reason=f"reset:{reason}",
        phase=str(state.get("pipeline_phase", "") or ""),
    )


# ---------------------------------------------------------------------------
# Escalation context (opens the mutation guard for REPLACE / EXTEND)
# ---------------------------------------------------------------------------


def begin_escalation(
    state: Any,
    *,
    escalation_type: str,
    reason: str,
    opened_by: str,
) -> None:
    """Open an escalation window that permits mutation of authoritative OTIO.

    REPLACE and EXTEND are the only escalation types that may re-derive
    an authoritative timeline (per ARCH-E1 spec).  Callers MUST pair
    this with :func:`end_escalation` once the re-derivation is complete.
    """
    if escalation_type not in {"REPLACE", "EXTEND"}:
        raise ValueError(
            f"Invalid escalation_type {escalation_type!r}; "
            "expected 'REPLACE' or 'EXTEND'"
        )
    if not reason or not opened_by:
        raise ValueError("begin_escalation: reason and opened_by are required")
    state[ESCALATION_KEY] = {
        "type": escalation_type,
        "reason": reason,
        "opened_by": opened_by,
    }
    logger.info(
        "otio_state: escalation OPENED type=%s by=%s reason=%s",
        escalation_type, opened_by, reason,
    )


def end_escalation(state: Any) -> None:
    """Close the escalation window."""
    prior = state.pop(ESCALATION_KEY, None)
    if prior:
        logger.info("otio_state: escalation CLOSED (was type=%s)", prior.get("type"))


def _current_escalation(state: Any) -> Optional[dict]:
    if state is None:
        return None
    esc = state.get(ESCALATION_KEY)
    return esc if isinstance(esc, dict) else None


# ---------------------------------------------------------------------------
# Mutation guard
# ---------------------------------------------------------------------------


def guard_authoritative_mutation(
    state: Any,
    *,
    operation: str,
    allow_escalation: bool = True,
) -> None:
    """Raise :class:`OtioStateViolation` if a mutation is attempted on
    authoritative OTIO.

    Call this at the top of any function that mutates the authoritative
    baseline of the timeline — narration clip boundaries, scene-level
    timing structure.  Downstream operations that merely BIND to the
    timeline (e.g. writing visual prompts into existing gap metadata,
    replacing an empty V1_Video gap with a generated clip whose
    source_range matches the gap) are not mutations of the authoritative
    baseline and do NOT need to call this guard.

    Args:
        state: blackboard (session state).  May be ``None`` during very
            early pipeline setup; the guard is a no-op in that case.
        operation: human-readable name of the mutating op, e.g.
            ``"clear_narration_track"`` or ``"add_narration_clip"``.
            Included verbatim in the structured failure.
        allow_escalation: when True (default), an open REPLACE/EXTEND
            escalation bypasses the guard.  Set False for operations
            that must NEVER mutate authoritative OTIO regardless of
            escalation state.
    """
    if state is None:
        return
    current = get_otio_state(state)
    if current != OTIO_STATE_AUTHORITATIVE:
        return

    escalation = _current_escalation(state)
    if allow_escalation and escalation is not None:
        logger.info(
            "otio_state: mutation '%s' permitted under escalation type=%s",
            operation, escalation.get("type"),
        )
        return

    details = {
        "operation": operation,
        "otio_state": current,
        "timeline_path": state.get("_timeline_path", ""),
        "escalation": escalation,
        "pipeline_phase": state.get("pipeline_phase", ""),
    }
    message = (
        f"OTIO STATE VIOLATION: attempt to mutate AUTHORITATIVE timeline "
        f"via '{operation}' without an open REPLACE/EXTEND escalation. "
        f"Downstream stages bind to the authoritative OTIO; they do not "
        f"mutate it. (phase={details['pipeline_phase']!r})"
    )
    logger.error(message)
    # Tag blackboard so dashboards/guardians can surface the violation
    # without parsing log lines.
    state["otio_violation"] = message
    raise OtioStateViolation(message, details)


# ---------------------------------------------------------------------------
# End-of-audio transition callback
# ---------------------------------------------------------------------------


def authoritative_transition_callback(
    callback_context: Any,
) -> Any:
    """``after_agent_callback`` that crystallises the timeline to authoritative.

    Wired onto the audio stage's reconciliation agent (``audio_agent``
    today; a future ``NarrationReconciliationAgent`` can reuse this
    callback).  Fires after the Timeline Guardian has already validated
    the post-audio OTIO state — once narration WAVs are measured and
    pacing is locked, the timeline becomes THE LAW for every downstream
    stage.

    Idempotent: calling it twice does not regress state, and calling it
    from a non-audio phase is a no-op.  Never raises — the transition
    is an advisory stage-boundary action, and the Timeline Guardian
    running earlier in the callback chain is the authoritative gate
    that blocks the pipeline on structural failures.
    """
    state = getattr(callback_context, "state", None)
    if state is None:
        logger.debug(
            "otio_state: transition_callback skipping — no state on ctx"
        )
        return None
    phase = state.get("pipeline_phase", "")

    if phase != "audio":
        logger.debug(
            "otio_state: transition_callback skipping — phase=%r != 'audio'",
            phase,
        )
        return None

    # Timeline Guardian writes ``state["otio_violation"]`` on failure and
    # clears it to None on pass.  If a violation is still sitting on the
    # blackboard, the timeline did NOT pass reconciliation — do not
    # crystallise a broken timeline as THE LAW.
    violation = state.get("otio_violation")
    if violation:
        logger.error(
            "otio_state: NOT crystallising — outstanding violation: %s",
            violation,
        )
        return None

    # ARCH-E2 (#148) + ARCH-E3 (#149): crystallise ONLY when every
    # block has passed BOTH timing reconciliation and stylistic QA.
    # These gates are boolean state keys written by the audio callback;
    # absence of a key means the corresponding check did not run (or
    # was rejected upstream) — either way we must not crystallise.
    # Use ``is not True`` (not ``is False``) so absent keys (value
    # ``None`` when the upstream gatekeeper short-circuited) block
    # crystallisation too — otherwise ``None is False`` is ``False``
    # and the gate silently lets the timeline through.
    # See docs/ARCHITECTURE_DIAGRAMS.md diagram 2 (stylistic QA +
    # crystallise).
    stylistic_passed = state.get("_stylistic_qa_passed")
    if stylistic_passed is not True:
        logger.error(
            "otio_state: NOT crystallising — stylistic QA (ARCH-E3) %s",
            "reported failures on at least one block"
            if stylistic_passed is False
            else "did not run (key absent) — cannot crystallise without a pass",
        )
        return None
    reconciliation_passed = state.get("_narration_reconciliation_passed")
    if reconciliation_passed is not True:
        logger.error(
            "otio_state: NOT crystallising — narration reconciliation "
            "(ARCH-E2) %s",
            "reported timing violations on at least one block"
            if reconciliation_passed is False
            else "did not run (key absent) — cannot crystallise without a pass",
        )
        return None

    if get_otio_state(state) == OTIO_STATE_AUTHORITATIVE:
        logger.info("otio_state: already authoritative — idempotent no-op")
        return None

    # Close any open escalation window: the re-derivation succeeded.
    if _current_escalation(state) is not None:
        end_escalation(state)

    set_otio_state(
        state,
        OTIO_STATE_AUTHORITATIVE,
        timeline_path=state.get("_timeline_path", ""),
        reason="end_of_audio_reconciliation",
        phase="audio",
    )
    logger.info(
        "otio_state: CRYSTALLISED — timeline is now AUTHORITATIVE "
        "(THE LAW above all downstream stages)"
    )
    return None
