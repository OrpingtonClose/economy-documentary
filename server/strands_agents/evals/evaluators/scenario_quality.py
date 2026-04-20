"""ScenarioQualityEvaluator — wraps deterministic structural checks.

Bridges the existing structural-check suite
(``server/tools/scenario_evaluator_checks.py``) into the
``strands-agents-evals`` Evaluator protocol. No LLM calls; no workers.

Input shape
-----------
``EvaluationData`` with:

* ``input``: the user prompt (``str``) — optional, aids topic-fidelity.
* ``actual_output``: a scenario ``dict`` with ``scenes`` and
  (optionally) ``style_lock`` / ``pronunciation_hints`` keys.
* ``metadata``: optional knobs — ``target_duration_sec`` (float),
  ``wpm`` (int), ``seconds_per_scene`` (int), ``start_verdict`` (str),
  ``pronunciation_whitelist`` (iterable[str]).

Output
------
One :class:`EvaluationOutput` per :class:`CheckResult` returned by
``run_all_structural_checks``. ``test_pass`` is tied to the cap the
check would apply: any check capping at ``POOR`` fails the case
(hard gate per ``CUSTOM_EVALUATORS.md`` §1).
"""

from __future__ import annotations

from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from tools.scenario_evaluator_checks import (
    CheckResult,
    EvaluatorReport,
    run_all_structural_checks,
)

# Verdicts ordered best -> worst. Any cap of POOR is a hard gate.
_HARD_FAIL_CAPS = frozenset({"POOR"})


class ScenarioQualityEvaluator(Evaluator[str, dict[str, Any]]):
    """Deterministic wrapper around structural scenario checks."""

    def __init__(self) -> None:
        super().__init__()

    def evaluate(
        self,
        evaluation_case: EvaluationData[str, dict[str, Any]],
    ) -> list[EvaluationOutput]:
        scenario = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        user_prompt = evaluation_case.input or ""

        report: EvaluatorReport = run_all_structural_checks(
            scenario,
            user_prompt=user_prompt,
            target_duration_sec=float(metadata.get("target_duration_sec", 0.0) or 0.0),
            wpm=int(metadata.get("wpm", 155)),
            seconds_per_scene=int(metadata.get("seconds_per_scene", 45)),
            pronunciation_whitelist=metadata.get("pronunciation_whitelist"),
            start_verdict=str(metadata.get("start_verdict", "EXCELLENT")),
        )

        return [_check_to_output(check) for check in report.results]


def _check_to_output(check: CheckResult) -> EvaluationOutput:
    hard_fail = (not check.passed) and check.verdict_cap in _HARD_FAIL_CAPS
    reason_prefix = "PASS" if check.passed else f"FAIL (cap={check.verdict_cap})"
    return EvaluationOutput(
        score=1.0 if check.passed else 0.0,
        test_pass=check.passed or not hard_fail,
        reason=f"{reason_prefix} {check.name}: {check.details}".strip(),
        label=check.name,
    )
