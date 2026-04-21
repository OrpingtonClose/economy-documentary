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
    INTERRUPT_GATE_CONFIG,
    ApprovalDecision,
    resume_command_from_decision,
    validate_decision,
)


def allowed_decisions_for(tool_name: str) -> set[str]:
    """Return the set of allowed ``decision.type`` strings for a tool.

    Args:
        tool_name: The interrupt-wrapped tool whose approval rules to
            look up (e.g. ``"launch_visual_production"``,
            ``"launch_assembly"``, ``"request_human_approval"``).

    Returns:
        Set such as ``{"accept", "edit", "reject", "respond"}``.
        Returns an empty set for tools not registered in the approval
        policy.
    """
    entry = INTERRUPT_GATE_CONFIG.get(tool_name)
    if entry is None:
        return set()
    return set(entry.get("allowed_decisions", ()))


__all__ = [
    "ApprovalDecision",
    "allowed_decisions_for",
    "resume_command_from_decision",
    "validate_decision",
]
