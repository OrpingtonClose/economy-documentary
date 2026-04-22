"""Unit tests for PR 5 — component-playground task adapters.

The :mod:`playground_tasks` experiment drives the HTTP surface; these
tests lock the underlying invariants that make the experiment worth
running:

* The replay ``_task`` callable is importable from every c01..c10
  experiment module.
* Every registry row in c01..c10 now declares a ``task_attr``.
* Case-name normalisation promotes metadata-only names to
  ``Case.name`` so the endpoints can address every case.
* The Strands experiment runs green end-to-end.
"""

from __future__ import annotations

import importlib

import pytest

from strands_agents.evals.experiments.playground_tasks import (
    PLAYGROUND_TASKS_EVALUATOR_THRESHOLDS,
    build_playground_tasks_experiment,
    playground_tasks_cases,
    playground_tasks_task,
)
from strands_agents.playground.registry import get_component, iter_components


TASK_ADAPTER_COMPONENTS: list[tuple[str, str, str]] = [
    ("c01", "strands_agents.evals.experiments.scenario", "scenario_task"),
    ("c02", "strands_agents.evals.experiments.timing", "timing_task"),
    (
        "c03",
        "strands_agents.evals.experiments.scenario_refiner",
        "scenario_refiner_task",
    ),
    ("c04", "strands_agents.evals.experiments.audio", "audio_task"),
    ("c05", "strands_agents.evals.experiments.timing_loop", "timing_loop_task"),
    (
        "c06",
        "strands_agents.evals.experiments.content_analyst",
        "content_analyst_task",
    ),
    (
        "c07",
        "strands_agents.evals.experiments.visual_concepter",
        "visual_concepter_task",
    ),
    (
        "c08",
        "strands_agents.evals.experiments.coherence_evaluator",
        "coherence_evaluator_task",
    ),
    ("c09", "strands_agents.evals.experiments.visual_loop", "visual_loop_task"),
    ("c10", "strands_agents.evals.experiments.production", "production_task"),
]


def test_experiment_runs_green() -> None:
    experiment = build_playground_tasks_experiment()
    reports = experiment.run_evaluations(task=playground_tasks_task)

    assert len(reports) == 1
    report = reports[0]
    threshold, hard_gate = PLAYGROUND_TASKS_EVALUATOR_THRESHOLDS[
        report.evaluator_name
    ]
    assert report.overall_score >= threshold, (
        f"{report.evaluator_name} scored {report.overall_score:.3f} "
        f"< {threshold} (hard_gate={hard_gate})"
    )


def test_cases_cover_every_row_1_and_row_2_component() -> None:
    case_names = {c.name for c in playground_tasks_cases()}
    for cid in [
        "c01",
        "c02",
        "c03",
        "c04",
        "c05",
        "c06",
        "c07",
        "c08",
        "c09",
        "c10",
    ]:
        assert any(cid in name for name in case_names), (
            f"{cid} must have at least one case in the corpus; got {case_names}"
        )


@pytest.mark.parametrize(
    "component_id,module_path,attr",
    TASK_ADAPTER_COMPONENTS,
    ids=[cid for cid, _, _ in TASK_ADAPTER_COMPONENTS],
)
def test_task_adapter_is_importable(
    component_id: str, module_path: str, attr: str
) -> None:
    module = importlib.import_module(module_path)
    task = getattr(module, attr)
    assert callable(task), f"{attr} on {module_path} must be callable"


@pytest.mark.parametrize(
    "component_id",
    [cid for cid, _, _ in TASK_ADAPTER_COMPONENTS],
)
def test_registry_wires_task_attr(component_id: str) -> None:
    component = get_component(component_id)
    assert component is not None
    assert component.task_attr is not None, (
        f"{component_id} must declare task_attr so /run can dispatch"
    )


def test_every_row_1_to_3_component_has_task_attr() -> None:
    # All 15 components now ship a task adapter — no NO_TASK_ADAPTER
    # gaps after PR 5. The playground frontend can rely on every
    # component being dispatchable.
    missing = [
        c.id for c in iter_components() if c.task_attr is None
    ]
    assert not missing, (
        f"components without task_attr after PR 5: {missing}"
    )


def test_case_name_normalisation_promotes_metadata_names() -> None:
    # c04 historically set its case name in metadata; the registry
    # promotes that to Case.name so endpoints can address each case.
    component = get_component("c04")
    assert component is not None
    cases = component.cases()
    assert cases, "c04 must ship cases"
    for idx, case in enumerate(cases):
        assert case.name, (
            f"c04 case #{idx} must have a non-empty name after "
            "registry normalisation"
        )
