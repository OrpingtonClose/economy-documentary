"""Unit tests for the playground run endpoint (PR 3).

Covers the four run statuses (``OK``, ``MODEL_UNREACHABLE``,
``NO_TASK_ADAPTER``, ``TASK_ERROR``) and the schema endpoint via the
strands-evals experiment runner — no pytest-driven assertions inside
the component, just the experiment harness.
"""

from __future__ import annotations

from strands_agents.evals.experiments.playground_run import (
    PLAYGROUND_RUN_EVALUATOR_THRESHOLDS,
    build_playground_run_experiment,
    playground_run_cases,
    playground_run_task,
)
from strands_agents.playground.registry import get_component, iter_components


def test_run_experiment_case_corpus_covers_every_status() -> None:
    names = {c.name for c in playground_run_cases()}
    # At least one case for each of the four run statuses plus
    # schema + 404 error paths.
    assert "deterministic_component_runs_ok" in names
    assert "custom_input_runs_ok" in names
    assert "llm_component_without_credentials_hard_fails" in names
    assert "unknown_component_returns_404" in names
    assert "missing_case_and_custom_returns_400" in names
    assert "unknown_case_name_returns_400" in names
    assert "schema_endpoint_returns_fields" in names
    assert "schema_endpoint_404_for_unknown" in names


def test_run_experiment_passes_every_case() -> None:
    exp = build_playground_run_experiment()
    reports = exp.run_evaluations(task=playground_run_task)
    assert len(reports) == 1
    report = reports[0]
    assert all(report.test_passes), [
        (name, reason)
        for name, passed, reason in zip(
            (c["name"] for c in report.cases),
            report.test_passes,
            report.reasons,
        )
        if not passed
    ]
    threshold, hard_gate = PLAYGROUND_RUN_EVALUATOR_THRESHOLDS[
        report.evaluator_name
    ]
    assert hard_gate is True
    assert report.overall_score >= threshold


def test_registry_task_attr_consistent_with_upstream_modules() -> None:
    """Every component that declares ``task_attr`` must export it."""
    for component in iter_components():
        if component.task_attr is None:
            continue
        task = component.task()
        assert task is not None, (
            f"{component.id} declares task_attr={component.task_attr!r} "
            f"but the upstream module did not export it"
        )
        assert callable(task)


def test_registry_task_returns_none_when_task_attr_unset() -> None:
    # After PR 5 every registered component declares a task_attr, so
    # the NO_TASK_ADAPTER fallback is exercised against a synthetic
    # ``Component`` with ``task_attr=None``. The fallback is kept as
    # a defensive surface for any future component added without a
    # task adapter — it must still resolve to ``None`` rather than
    # raising.
    from strands_agents.playground.registry import Component

    synthetic = Component(
        id="c_synthetic_no_task",
        title="Synthetic",
        kind="leaf",
        row=1,
        summary="",
        experiment_module="strands_agents.evals.experiments.scenario",
        cases_factory="scenario_cases",
        thresholds_attr="SCENARIO_EVALUATOR_THRESHOLDS",
        declared_models=(),
        task_attr=None,
    )
    assert synthetic.task_attr is None
    assert synthetic.task() is None
