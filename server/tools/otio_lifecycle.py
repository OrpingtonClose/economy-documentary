"""
Stateless file-based OTIO lifecycle state management.

The OTIO file itself is the single source of truth for lifecycle state —
no blackboard, no in-memory dict.  Every read hits disk; every write
goes through ``otio_read_modify_write`` so the file is always consistent.

Lifecycle states
----------------
- ``draft``          — timeline is mutable; pipeline stages may freely
                        add / remove / reorder clips.
- ``authoritative`` — timeline has crystallised after audio
                        reconciliation.  Mutations are forbidden unless
                        an escalation (REPLACE / EXTEND) is open.

Escalation
----------
An escalation window (REPLACE or EXTEND) temporarily re-opens mutation
access on an authoritative timeline.  The escalation record lives in
``timeline.metadata["documentary"]["escalation"]`` and must be opened
via :func:`begin_escalation` and closed via :func:`end_escalation`.

This module is the file-based counterpart to
:mod:`callbacks.otio_state` (which keeps state on the ADK blackboard).
Both enforce the same ARCH-E1 invariant; this one is suitable for
Strands agents and any context where a session-state dict is
unavailable.

Spec references:
    - Issue #147 (ARCH-E1 Draft → Authoritative formal state transition)
    - Parent issue #127 (ARCH-E Authoritative OTIO + reconciliation + QA)
    - Meta issue #122 (ARCH-2026 architecture conformance)
"""

from __future__ import annotations

import copy
import json
import logging
import time
from typing import Any, Optional

from tools.otio_file_ops import otio_read, otio_read_modify_write

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OTIO_STATE_DRAFT = "draft"
OTIO_STATE_AUTHORITATIVE = "authoritative"

_VALID_STATES = frozenset({OTIO_STATE_DRAFT, OTIO_STATE_AUTHORITATIVE})

_VALID_ESCALATION_TYPES = frozenset({"REPLACE", "EXTEND"})


# ---------------------------------------------------------------------------
# Structured failure
# ---------------------------------------------------------------------------


class OtioStateViolation(Exception):
    """Raised when a caller tries to mutate an authoritative OTIO timeline
    without an open escalation window.

    Carries a structured ``details`` dict so log/telemetry consumers can
    reason about the violation without parsing the message string.

    Attributes:
        details: Dict with keys ``operation``, ``otio_state``,
            ``escalation``, and optionally ``timeline_path``.
    """

    def __init__(self, message: str, details: dict) -> None:
        super().__init__(message)
        self.details = details


# ---------------------------------------------------------------------------
# State readers
# ---------------------------------------------------------------------------


def get_otio_lifecycle_state(timeline_path: str) -> str:
    """Read the current OTIO lifecycle state from the timeline file on disk.

    Reads ``timeline.metadata["documentary"]["state"]``.  Returns
    ``"draft"`` if the key is missing (backwards compat with timelines
    created before the lifecycle field was introduced).

    Args:
        timeline_path: Absolute path to the ``.otio`` timeline file.

    Returns:
        One of ``"draft"`` or ``"authoritative"``.
    """
    timeline = otio_read(timeline_path)
    doc_meta = timeline.metadata.get("documentary", {})
    state = doc_meta.get("state", OTIO_STATE_DRAFT)
    if state not in _VALID_STATES:
        logger.warning(
            "otio_lifecycle: unknown state %r on %s — defaulting to draft",
            state, timeline_path,
        )
        return OTIO_STATE_DRAFT
    return state


# ---------------------------------------------------------------------------
# State writers
# ---------------------------------------------------------------------------


def set_otio_lifecycle_state(
    timeline_path: str,
    new_state: str,
    reason: str = "",
) -> str:
    """Transition the OTIO lifecycle state via read-modify-write.

    Validates the transition and records a history entry in
    ``timeline.metadata["documentary"]["state_history"]`` as a list of
    ``{from, to, reason, timestamp}`` dicts.

    Transition rules:
        - ``draft`` → ``authoritative``: always allowed.
        - ``authoritative`` → ``draft``: only allowed when an escalation
          is currently open (REPLACE / EXTEND).  Without an escalation
          the transition is rejected to prevent accidental regression.

    Args:
        timeline_path: Absolute path to the ``.otio`` timeline file.
        new_state: Target state (``"draft"`` or ``"authoritative"``).
        reason: Human-readable reason for the transition (audit trail).

    Returns:
        JSON string with ``{prior, new, reason}``.

    Raises:
        ValueError: If *new_state* is not a valid state.
        OtioStateViolation: If reverting from authoritative to draft
            without an open escalation.
    """
    if new_state not in _VALID_STATES:
        raise ValueError(
            f"Invalid OTIO state {new_state!r}; "
            f"expected one of {sorted(_VALID_STATES)}"
        )

    prior = get_otio_lifecycle_state(timeline_path)

    # Guard: authoritative → draft requires an open escalation.
    if prior == OTIO_STATE_AUTHORITATIVE and new_state == OTIO_STATE_DRAFT:
        escalation = get_escalation(timeline_path)
        if escalation is None:
            raise OtioStateViolation(
                f"Cannot revert from authoritative to draft on {timeline_path} "
                f"without an open escalation. Open one via begin_escalation().",
                details={
                    "operation": "set_otio_lifecycle_state",
                    "otio_state": prior,
                    "escalation": None,
                    "timeline_path": timeline_path,
                },
            )

    def _mutator(timeline: Any) -> None:
        """Apply the state transition and append history entry."""

        doc_meta = timeline.metadata.setdefault("documentary", {})
        doc_meta["state"] = new_state
        if reason:
            doc_meta["state_reason"] = reason

        history = doc_meta.get("state_history", [])
        history.append({
            "from": prior,
            "to": new_state,
            "reason": reason,
            "timestamp": time.time(),
        })
        doc_meta["state_history"] = history

    otio_read_modify_write(timeline_path, _mutator)

    logger.info(
        "otio_lifecycle: %s -> %s (reason=%s) on %s",
        prior, new_state, reason, timeline_path,
    )

    return json.dumps({"prior": prior, "new": new_state, "reason": reason})


# ---------------------------------------------------------------------------
# Escalation management
# ---------------------------------------------------------------------------


def begin_escalation(
    timeline_path: str,
    escalation_type: str,
    reason: str,
    opened_by: str,
) -> str:
    """Open an escalation window on the timeline file.

    Writes the escalation record to
    ``timeline.metadata["documentary"]["escalation"]`` via RMW.

    Valid escalation types: ``"REPLACE"``, ``"EXTEND"``.

    Args:
        timeline_path: Absolute path to the ``.otio`` timeline file.
        escalation_type: One of ``"REPLACE"`` or ``"EXTEND"``.
        reason: Human-readable reason for the escalation.
        opened_by: Identifier of the agent / user opening the escalation.

    Returns:
        JSON string with ``{opened: True, type, reason}``.

    Raises:
        ValueError: If *escalation_type* is not valid, or *reason* /
            *opened_by* are empty.
    """
    if escalation_type not in _VALID_ESCALATION_TYPES:
        raise ValueError(
            f"Invalid escalation_type {escalation_type!r}; "
            f"expected one of {sorted(_VALID_ESCALATION_TYPES)}"
        )
    if not reason or not opened_by:
        raise ValueError(
            "begin_escalation: 'reason' and 'opened_by' are required"
        )

    record = {
        "type": escalation_type,
        "reason": reason,
        "opened_by": opened_by,
        "opened_at": time.time(),
    }

    def _mutator(timeline: Any) -> None:
        doc_meta = timeline.metadata.setdefault("documentary", {})
        doc_meta["escalation"] = record

    otio_read_modify_write(timeline_path, _mutator)

    logger.info(
        "otio_lifecycle: escalation OPENED type=%s by=%s reason=%s on %s",
        escalation_type, opened_by, reason, timeline_path,
    )

    return json.dumps({"opened": True, "type": escalation_type, "reason": reason})


def end_escalation(timeline_path: str) -> str:
    """Close the escalation window on the timeline file.

    Clears ``timeline.metadata["documentary"]["escalation"]`` via RMW.

    Args:
        timeline_path: Absolute path to the ``.otio`` timeline file.

    Returns:
        JSON string with ``{closed: True}``.
    """

    def _mutator(timeline: Any) -> None:
        doc_meta = timeline.metadata.setdefault("documentary", {})
        doc_meta.pop("escalation", None)

    otio_read_modify_write(timeline_path, _mutator)

    logger.info("otio_lifecycle: escalation CLOSED on %s", timeline_path)

    return json.dumps({"closed": True})


def get_escalation(timeline_path: str) -> Optional[dict]:
    """Read the current escalation record from the timeline file on disk.

    Args:
        timeline_path: Absolute path to the ``.otio`` timeline file.

    Returns:
        The escalation dict (with keys ``type``, ``reason``,
        ``opened_by``, ``opened_at``) if an escalation is open, or
        ``None`` if no escalation is recorded.
    """
    timeline = otio_read(timeline_path)
    doc_meta = timeline.metadata.get("documentary", {})
    esc = doc_meta.get("escalation")
    if esc is None:
        return None
    # OTIO AnyDictionary is not a Python dict — use copy.deepcopy
    return copy.deepcopy(dict(esc))


# ---------------------------------------------------------------------------
# Mutation guard
# ---------------------------------------------------------------------------


def guard_mutation(
    timeline_path: str,
    operation: str,
    allow_escalation: bool = True,
) -> None:
    """Raise :class:`OtioStateViolation` if a mutation is not permitted.

    Reads the lifecycle state and escalation status from disk (the OTIO
    file is the single source of truth).

    Rules:
        - **Draft** state: mutations are always allowed.
        - **Authoritative** with an open escalation: mutations are
          allowed if *allow_escalation* is ``True``.
        - **Authoritative** without an escalation: mutations are
          forbidden — raises :class:`OtioStateViolation`.

    Args:
        timeline_path: Absolute path to the ``.otio`` timeline file.
        operation: Human-readable name of the mutating operation (e.g.
            ``"add_narration_clip"``).  Included in the violation details.
        allow_escalation: When ``True`` (default), an open escalation
            bypasses the guard.  Set to ``False`` for operations that
            must never mutate authoritative OTIO regardless of
            escalation state.

    Raises:
        OtioStateViolation: If the mutation is forbidden.
    """
    current = get_otio_lifecycle_state(timeline_path)

    # Draft state: mutations always allowed.
    if current != OTIO_STATE_AUTHORITATIVE:
        return

    escalation = get_escalation(timeline_path)

    # Authoritative with escalation: allowed if caller permits it.
    if allow_escalation and escalation is not None:
        logger.info(
            "otio_lifecycle: mutation '%s' permitted under escalation "
            "type=%s on %s",
            operation, escalation.get("type"), timeline_path,
        )
        return

    # Authoritative without escalation: forbidden.
    details = {
        "operation": operation,
        "otio_state": current,
        "escalation": escalation,
        "timeline_path": timeline_path,
    }
    message = (
        f"OTIO STATE VIOLATION: attempt to mutate AUTHORITATIVE timeline "
        f"via '{operation}' without an open REPLACE/EXTEND escalation. "
        f"Downstream stages bind to the authoritative OTIO; they do not "
        f"mutate it. (path={timeline_path!r})"
    )
    logger.error(message)
    raise OtioStateViolation(message, details)
