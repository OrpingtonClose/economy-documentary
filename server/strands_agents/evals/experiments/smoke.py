"""Phase-1 smoke experiment.

Exercises the :class:`Experiment` lifecycle end-to-end against a
deterministic evaluator so ``.github/workflows/strands-evals.yml`` can
catch wiring regressions (serialization, evaluator loading,
``run_evaluations`` contract) before a component PR lands.

The experiment intentionally uses :class:`ContractComplianceEvaluator`
(zero LLM calls, deterministic output) so it runs offline on CI. A
separate LLM-backed smoke lands in component 01 once model credentials
are wired in.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.experiment import Experiment

from contracts import StageContract
from strands_agents.evals.evaluators import ContractComplianceEvaluator

_SMOKE_CONTRACT = StageContract(
    name="smoke_contract",
    required_state=["topic"],
    produced_state=["scenario"],
    produced_artifacts=[],
)

_CASES: list[Case] = [
    Case(
        name="contract_all_present",
        input={"topic": "inflation basics"},
        expected_output={"topic": "inflation basics", "scenario": {"scenes": []}},
        metadata={"contract_name": _SMOKE_CONTRACT.name},
    ),
    Case(
        name="contract_missing_required",
        input={},
        expected_output={"scenario": {"scenes": []}},
        metadata={"contract_name": _SMOKE_CONTRACT.name},
    ),
]


def build_smoke_experiment() -> Experiment:
    """Construct the smoke :class:`Experiment`.

    Returns:
        An :class:`Experiment` wired with a
        :class:`ContractComplianceEvaluator` for ``_SMOKE_CONTRACT``
        and two cases — one that should pass every clause, one that
        should fail on the missing ``topic``.
    """
    return Experiment(
        cases=_CASES,
        evaluators=[ContractComplianceEvaluator(_SMOKE_CONTRACT)],
    )


def smoke_task(case: Case) -> dict[str, Any]:
    """Task function used by :func:`Experiment.run_evaluations`.

    Returns the :class:`Experiment` task-protocol wrapper
    ``{"output": ..., "trajectory": ...}`` so that ``actual_output``
    on the evaluator's :class:`EvaluationData` is the state dict
    itself, not a wrapping mapping. Two cases, symmetric structure:
    the second intentionally omits ``topic`` so the evaluator reports
    a required-state failure.
    """
    return {"output": case.expected_output or {}}
