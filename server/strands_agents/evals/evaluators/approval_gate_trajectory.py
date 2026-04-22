"""ApprovalGateTrajectoryEvaluator — HITL interrupt behaviour check.

Validates that a human-in-the-loop ``interrupt_on`` tool went through
the expected approval dance and that the orchestrator responded to the
human's decision correctly.

Input shape
-----------
``EvaluationData`` with:

* ``actual_trajectory``: ``list[dict]`` of tool-call and interrupt
  records. Interrupt records are identified by ``"kind": "interrupt"``
  and carry ``tool``, ``decision`` (one of ``accept`` / ``edit`` /
  ``reject`` / ``respond`` — the legacy ``approve`` alias is still
  accepted), and optional ``args``.
* ``metadata[`gated_tool`]`` (required): the tool that should have
  been gated by an interrupt (e.g. ``"launch_b2_sync"``).
* ``metadata[`expected_decision`]`` (required): one of ``accept`` /
  ``edit`` / ``reject`` / ``respond`` (``approve`` is accepted as an
  alias for ``accept``). Encodes the scripted operator response.
* ``metadata[`post_approval_tool`]`` (optional): when the scripted
  decision admits a follow-through (``accept`` / ``edit``), the
  orchestrator must subsequently call this tool (typically the same
  as ``gated_tool`` — the approved version). Ignored on ``reject``
  or ``respond``, which never permit follow-through.
* ``metadata[`forbidden_on_reject`]`` (optional): extra tools that
  must never be called on reject/respond (e.g. downstream publishers).

Output
------
One :class:`EvaluationOutput` per check performed. Hard gate: every
check must pass.
"""

from __future__ import annotations

from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

# Canonical decision vocabulary matches :data:`INTERRUPT_GATE_CONFIG`.
# ``approve`` is accepted as a legacy alias for ``accept`` so cases
# authored before component 15 still score.
_VALID_DECISIONS = frozenset({"accept", "edit", "reject", "respond", "approve"})
_FOLLOW_THROUGH_DECISIONS = frozenset({"accept", "edit", "approve"})


def _normalize(decision: str) -> str:
    """Collapse the legacy ``approve`` alias onto ``accept``."""

    return "accept" if decision == "approve" else decision


class ApprovalGateTrajectoryEvaluator(Evaluator[Any, Any]):
    """Check an HITL interrupt ran and was honoured correctly."""

    def __init__(self) -> None:
        super().__init__()

    def evaluate(
        self,
        evaluation_case: EvaluationData[Any, Any],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}
        gated_tool = metadata.get("gated_tool")
        expected_decision = metadata.get("expected_decision")
        post_approval_tool = metadata.get("post_approval_tool")
        forbidden_on_reject = list(metadata.get("forbidden_on_reject") or [])

        if not gated_tool or expected_decision not in _VALID_DECISIONS:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        "metadata requires 'gated_tool' and "
                        "'expected_decision' in "
                        "{accept, edit, reject, respond} "
                        "(legacy 'approve' alias also accepted)"
                    ),
                    label="approval.missing_config",
                )
            ]

        trajectory = evaluation_case.actual_trajectory
        if not isinstance(trajectory, list):
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="actual_trajectory must be list[dict]",
                    label="approval.missing_actual",
                )
            ]

        interrupt_record = _find_interrupt(trajectory, gated_tool)
        outputs: list[EvaluationOutput] = []

        if interrupt_record is None:
            outputs.append(
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=f"FAIL no interrupt raised for gated tool {gated_tool!r}",
                    label="approval.raised",
                )
            )
            return outputs

        outputs.append(
            EvaluationOutput(
                score=1.0,
                test_pass=True,
                reason=f"PASS interrupt raised for {gated_tool}",
                label="approval.raised",
            )
        )

        actual_decision = _normalize(str(interrupt_record.get("decision", "")).lower())
        normalized_expected = _normalize(expected_decision)
        decision_ok = actual_decision == normalized_expected
        outputs.append(
            EvaluationOutput(
                score=1.0 if decision_ok else 0.0,
                test_pass=decision_ok,
                reason=(
                    f"PASS decision {actual_decision!r} matches expected"
                    if decision_ok
                    else f"FAIL decision {actual_decision!r}, expected {normalized_expected!r}"
                ),
                label="approval.decision",
            )
        )

        called_tools = {
            call.get("name")
            for call in trajectory
            if isinstance(call, dict)
            and call.get("kind", "tool_call") == "tool_call"
        }

        if normalized_expected in _FOLLOW_THROUGH_DECISIONS:
            expected_follow = post_approval_tool or gated_tool
            follow_ok = expected_follow in called_tools
            outputs.append(
                EvaluationOutput(
                    score=1.0 if follow_ok else 0.0,
                    test_pass=follow_ok,
                    reason=(
                        f"PASS {expected_follow} invoked after approval"
                        if follow_ok
                        else f"FAIL {expected_follow} not invoked after approval"
                    ),
                    label="approval.followthrough",
                )
            )
        else:
            # reject + respond both short-circuit the gated tool.
            forbidden = {gated_tool, *forbidden_on_reject}
            if post_approval_tool:
                forbidden.add(post_approval_tool)
            # The gated tool itself should appear ONLY as the interrupt
            # request, not as a completed tool_call — so we count
            # tool_call kind only, via `called_tools`.
            leaked = sorted(forbidden & called_tools)
            outputs.append(
                EvaluationOutput(
                    score=1.0 if not leaked else 0.0,
                    test_pass=not leaked,
                    reason=(
                        "PASS no gated/forbidden tools called after rejection"
                        if not leaked
                        else f"FAIL called despite rejection: {leaked}"
                    ),
                    label="approval.no_leak",
                )
            )
        return outputs


def _find_interrupt(trajectory: list[Any], gated_tool: str) -> dict[str, Any] | None:
    for record in trajectory:
        if not isinstance(record, dict):
            continue
        if record.get("kind") != "interrupt":
            continue
        if record.get("tool") == gated_tool:
            return record
    return None
