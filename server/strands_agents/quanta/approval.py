"""Component 15 atoms — approval-gate pure helpers.

Three pure atoms extracted from ``approval.py``:

* :func:`validate_decision` — raise :class:`ValueError` if an
  operator decision is malformed for the given tool.
* :func:`resume_command_from_decision` — turn a validated decision
  into a LangGraph :class:`~langgraph.types.Command`.
* :func:`allowed_decisions_for` — return the set of allowed decision
  types (``accept`` / ``edit`` / ``reject``) for a given tool name.

The rest of the approval machinery (the ``interrupt_on`` wrapping, the
operator IO, the blocking resume protocol) is a connector — it bridges
the graph runtime to a human operator.
"""

from __future__ import annotations

from strands_agents.approval import (
    ApprovalDecision,
    _allowed_decisions,
    resume_command_from_decision,
    validate_decision,
)


def allowed_decisions_for(tool_name: str) -> set[str]:
    """Return the set of allowed ``decision.type`` strings for a tool.

    Delegates to the same internal helper
    :func:`strands_agents.approval._allowed_decisions` that
    :func:`validate_decision` uses. This guarantees the two never
    disagree: any ``decision.type`` that this helper advertises will
    also pass the validator for the same tool, including when an
    ``INTERRUPT_GATE_CONFIG`` entry omits the ``allowed_decisions`` key
    or when the tool is not registered at all (both fall back to the
    permissive ``{accept, edit, reject, respond}`` superset).

    Args:
        tool_name: The interrupt-wrapped tool whose approval rules to
            look up (e.g. ``"launch_visual_production"``,
            ``"launch_assembly"``, ``"request_human_approval"``).

    Returns:
        Set of decision-type strings allowed for the tool.
    """
    return set(_allowed_decisions(tool_name))


__all__ = [
    "ApprovalDecision",
    "allowed_decisions_for",
    "resume_command_from_decision",
    "validate_decision",
]
