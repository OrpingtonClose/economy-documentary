"""Unit coverage for the playground evaluator endpoint (PR 4).

Locks the endpoint's contract via the Strands experiment so every
asserted case is protected as a unit test, not just inside the eval
harness. Also pins the registry side: every component's evaluator
builder must at least be importable, even if it returns no
evaluators.
"""

from __future__ import annotations

import pytest

from strands_agents.evals.experiments.playground_evaluate import (
    PLAYGROUND_EVALUATE_EVALUATOR_THRESHOLDS,
    build_playground_evaluate_experiment,
    playground_evaluate_cases,
    playground_evaluate_task,
)
from strands_agents.playground.registry import iter_components


def test_experiment_runs_green() -> None:
    experiment = build_playground_evaluate_experiment()
    reports = experiment.run_evaluations(task=playground_evaluate_task)

    assert len(reports) == 1
    report = reports[0]
    threshold, hard_gate = PLAYGROUND_EVALUATE_EVALUATOR_THRESHOLDS[
        report.evaluator_name
    ]
    assert report.overall_score >= threshold, (
        f"{report.evaluator_name} scored {report.overall_score:.3f} "
        f"< {threshold} (hard_gate={hard_gate})"
    )


def test_cases_cover_every_status() -> None:
    expected_cases = {
        "approval_canonical_output_passes",
        "empty_output_hard_gates_fail",
        "custom_input_evaluates_without_expected_output",
        "unknown_component_returns_404",
        "unknown_case_name_returns_400",
        "missing_case_and_custom_returns_400",
        "missing_actual_output_returns_422",
        "custom_expected_overrides_case_expected",
    }
    actual_names = {case.name for case in playground_evaluate_cases()}
    assert expected_cases == actual_names


def test_cases_are_non_trivial() -> None:
    cases = playground_evaluate_cases()
    assert len(cases) >= 8
    for case in cases:
        assert case.input, f"case {case.name} has empty input"
        assert case.expected_output, (
            f"case {case.name} has empty expected_output"
        )


@pytest.mark.parametrize(
    "component",
    list(iter_components()),
    ids=lambda c: c.id,
)
def test_component_evaluator_builder_importable(component: object) -> None:
    # ``evaluator_instances`` must either return a list of evaluators
    # or an empty list. It must never raise. Regression guard: an
    # import failure during early registry wiring would silently
    # blank the evaluator endpoint for that component, which is
    # worse than surfacing an empty list.
    instances = component.evaluator_instances()  # type: ignore[attr-defined]
    assert isinstance(instances, list)
