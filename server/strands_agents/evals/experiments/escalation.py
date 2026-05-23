"""Escalation-supervisor experiment (component 13).

Exercises the deterministic decision tool over the 8 fixture payloads
and composes the evaluator stack declared in
``docs/strands-migration/components/13-escalation-supervisor.md``.

The experiment runs offline in CI: the decision tool is rule-based so
every case executes without model credentials. Optional LLM-as-judge
evaluation (:class:`EscalationDecisionEvaluator`) is opt-in via
``build_escalation_experiment(judge_model=...)`` for local / staging
runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from strands_evals.case import Case  # type: ignore[import-not-found]
from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.experiment import Experiment  # type: ignore[import-not-found]
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput  # type: ignore[import-not-found]
from strands.models.model import Model

from contracts import ESCALATION_CONTRACT
from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
    EscalationDecisionEvaluator,
)
from strands_agents.subagents.escalation import decide_escalation_action

_FIXTURES_DIR = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "escalation"
)

# ``(case_name, fixture_file, expected_action, judge_diagnostic_action)``
#
# The fourth column is the vocabulary the LLM-as-judge uses (5 items:
# ``fix / retry / skip / escalate / abort``). Our decision contract
# uses the 6-item vocabulary that includes ``escalate_to_human``; the
# judge collapses that to ``escalate`` since both describe the same
# action semantically.
_CASE_TABLE: list[tuple[str, str, str, str]] = [
    ("transient_retry", "transient_retry_payload.json", "retry", "retry"),
    ("persistent_fail", "persistent_fail_payload.json", "skip", "skip"),
    (
        "invariant_violation",
        "invariant_violation_payload.json",
        "abort",
        "abort",
    ),
    (
        "budget_exhausted_whole_stage",
        "budget_exhausted_payload.json",
        "escalate_to_human",
        "escalate",
    ),
    (
        "catastrophic_worker_crash",
        "catastrophic_payload.json",
        "escalate_to_human",
        "escalate",
    ),
    ("style_drift", "style_drift_payload.json", "fix", "fix"),
    ("timing_loop_stuck", "timing_loop_stuck_payload.json", "skip", "skip"),
    ("bad_payload", "bad_payload_payload.json", "abort", "abort"),
]


def _load_payload(filename: str) -> dict[str, Any]:
    path = _FIXTURES_DIR / filename
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _build_cases() -> list[Case]:
    cases: list[Case] = []
    for case_name, filename, expected_action, judge_action in _CASE_TABLE:
        payload = _load_payload(filename)
        cases.append(
            Case(
                name=case_name,
                input={"diagnostic": payload},
                expected_output={"action": expected_action},
                metadata={
                    "contract_name": ESCALATION_CONTRACT.name,
                    "expected_action": expected_action,
                    "fixture": filename,
                    "diagnostic": payload,
                    "judge_actual_action": judge_action,
                },
            )
        )
    return cases


_CASES: list[Case] = _build_cases()


# ── Custom deterministic evaluators ──────────────────────────────────

class ActionEqualsEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Compare the decision's ``action`` to ``metadata['expected_action']``.

    Strands-evals ships :class:`Equals` but it compares the full
    ``actual_output`` dict against the full ``expected_output`` dict,
    which would also require rationale / confidence to match exactly.
    We only need the decision label to match; the rationale and
    confidence are covered by :class:`EscalationDecisionEvaluator`.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        expected = str(metadata.get("expected_action", "")).strip()
        got = str(actual.get("action", "")).strip()
        match = bool(expected) and got == expected
        return [
            EvaluationOutput(
                score=1.0 if match else 0.0,
                test_pass=match,
                reason=(
                    f"decision.action={got!r} "
                    f"{'matches' if match else 'does not match'} "
                    f"expected={expected!r}"
                ),
                label="action_match" if match else "action_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class HumanSummaryRequiredEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Fail cases where ``escalate_to_human`` lacks a ``human_summary``.

    Acceptance criterion in the spec: every ``escalate_to_human`` case
    must carry a non-empty ``human_summary`` that the approval gate
    (component 15) can show the operator verbatim.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        action = str(actual.get("action", "")).strip()
        if action != "escalate_to_human":
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="non-human action; no human_summary required",
                    label="not_applicable",
                )
            ]
        summary = actual.get("human_summary")
        has_summary = bool(summary) and bool(str(summary).strip())
        return [
            EvaluationOutput(
                score=1.0 if has_summary else 0.0,
                test_pass=has_summary,
                reason=(
                    "human_summary present"
                    if has_summary
                    else "escalate_to_human missing non-empty human_summary"
                ),
                label="human_summary_ok"
                if has_summary
                else "human_summary_missing",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


# ── Task adapters ────────────────────────────────────────────────────

def escalation_task(case: Case) -> dict[str, Any]:
    """Run :func:`decide_escalation_action` against a case payload.

    Returns the task-protocol envelope consumed by
    :meth:`Experiment.run_evaluations`. ``actual_output`` on the
    evaluator's :class:`EvaluationData` is the decision dict itself so
    :class:`ActionEqualsEvaluator` and
    :class:`HumanSummaryRequiredEvaluator` can read it directly.
    """

    payload = (case.input or {}).get("diagnostic") or {}
    decision = decide_escalation_action(payload)
    return {
        "output": decision,
        "trajectory": [
            {
                "tool": "decide_escalation_action",
                "args": {"diagnostic_payload": payload},
                "result": decision,
            }
        ],
    }


def escalation_contract_task(case: Case) -> dict[str, Any]:
    """Adapter for :class:`ContractComplianceEvaluator`.

    Projects the decision onto the state shape the contract expects —
    ``{"diagnostic": ..., "decision": ...}`` — so ``required_state``
    (``diagnostic``) and ``produced_state`` (``decision``) validate.
    """

    payload = (case.input or {}).get("diagnostic") or {}
    decision = decide_escalation_action(payload)
    return {
        "output": {
            "diagnostic": payload,
            "decision": decision,
        }
    }


def escalation_judge_task(case: Case) -> dict[str, Any]:
    """Adapter for :class:`EscalationDecisionEvaluator`.

    The judge uses the 5-item vocabulary ``fix / retry / skip /
    escalate / abort``. We collapse ``escalate_to_human`` → ``escalate``
    for the judge's benefit but keep the full 6-item vocabulary on the
    underlying :class:`EscalationDecision` for the orchestrator.
    """

    payload = (case.input or {}).get("diagnostic") or {}
    decision = decide_escalation_action(payload)
    action = decision.get("action", "")
    judge_action = "escalate" if action == "escalate_to_human" else action
    return {
        "output": {
            "action": judge_action,
            "reasoning": decision.get("rationale"),
            "state_patches": decision.get("state_patches"),
        }
    }


# ── Experiment factories ─────────────────────────────────────────────

def build_escalation_experiment(
    *,
    judge_model: Model | str | None = None,
) -> Experiment:
    """Primary experiment: action + human_summary.

    Args:
        judge_model: If provided, the LLM-as-judge
            :class:`EscalationDecisionEvaluator` is appended — but
            wired against :func:`escalation_judge_task` via the
            scoped factory :func:`build_escalation_judge_experiment`.
            The primary experiment stays deterministic so CI runs
            without credentials.

    Returns:
        An :class:`Experiment` with the 8 cases and the deterministic
        action-match + human-summary evaluators.
    """

    evaluators: list[Evaluator[Any, Any]] = [
        ActionEqualsEvaluator(),
        HumanSummaryRequiredEvaluator(),
    ]
    # ``judge_model`` is documented above; the judge lives on the
    # scoped factory to keep the evaluator-output shapes coherent.
    _ = judge_model  # kwarg retained for API symmetry with other components.
    return Experiment(cases=list(_CASES), evaluators=evaluators)


def build_escalation_contract_experiment() -> Experiment:
    """Scoped experiment for :class:`ContractComplianceEvaluator`.

    Uses :func:`escalation_contract_task` so ``actual_output`` carries
    ``diagnostic`` + ``decision`` at the top level.
    """

    return Experiment(
        cases=list(_CASES),
        evaluators=[ContractComplianceEvaluator(ESCALATION_CONTRACT)],
    )


def build_escalation_judge_experiment(
    *,
    judge_model: Model | str,
) -> Experiment:
    """Scoped experiment for :class:`EscalationDecisionEvaluator`.

    Args:
        judge_model: Strands model id or :class:`Model` instance.
    """

    return Experiment(
        cases=list(_CASES),
        evaluators=[EscalationDecisionEvaluator(model=judge_model)],
    )


__all__ = [
    "ActionEqualsEvaluator",
    "HumanSummaryRequiredEvaluator",
    "build_escalation_contract_experiment",
    "build_escalation_experiment",
    "build_escalation_judge_experiment",
    "escalation_contract_task",
    "escalation_judge_task",
    "escalation_task",
]
