"""Component 13 atom — escalation decision.

One pure atom: :func:`decide_escalation_action`. Deterministic rule
table over a diagnostic payload — produces an escalation decision
(``retry``, ``skip``, ``escalate_to_human``, ``abort``) with target,
rationale, confidence, optional human summary, and optional state
patches.

The SubAgent that surrounds this atom is a connector: it invokes the
tool via the LLM, enforces the ``human_summary`` contract, and
persists the decision. Pure rule evaluation is atomic.
"""

from __future__ import annotations

from typing import Any

from strands_agents.subagents.escalation import (
    decide_escalation_action as _decide_escalation_action_tool,
)


def decide_escalation_action(diagnostic_payload: dict[str, Any]) -> dict[str, Any]:
    """Classify a diagnostic payload into an escalation decision.

    Args:
        diagnostic_payload: Structured payload produced by the parent
            orchestrator after tactical recovery gave up. See
            ``docs/strands-migration/components/13-escalation-supervisor.md``
            for the schema.

    Returns:
        ``{"action": "retry|skip|escalate_to_human|abort",
           "target": {"scope": ..., "id": ...},
           "rationale": str, "confidence": float,
           "human_summary": str | None,
           "state_patches": dict | None}``.
    """
    return _decide_escalation_action_tool.__wrapped__(diagnostic_payload)


__all__ = ["decide_escalation_action"]
