"""Unit tests for the Component Playground catalog.

PR 1 of ``docs/strands-migration/plans/component-playground.md`` lands
read-only endpoints plus a strands-evals experiment that exercises
them end-to-end against a FastAPI ``TestClient``. These tests assert:

* the registry enumerates all 15 atomic components in atlas order;
* the catalog experiment's cases match the endpoints they call;
* every case in the experiment passes the deterministic evaluator.

A regression here means either the catalog endpoints drift from the
registry, or the registry drifts from the upstream experiment
modules — both are shipped as a single coherent surface.
"""

from __future__ import annotations

from strands_agents.evals.experiments.playground_catalog import (
    PLAYGROUND_CATALOG_EVALUATOR_THRESHOLDS,
    build_playground_catalog_experiment,
    playground_catalog_task,
)
from strands_agents.playground import COMPONENT_IDS, iter_components


def test_registry_enumerates_all_fifteen_components_in_atlas_order() -> None:
    ids = tuple(c.id for c in iter_components())
    assert ids == COMPONENT_IDS
    assert len(ids) == 15


def test_every_component_declares_kind_and_row() -> None:
    for component in iter_components():
        assert component.kind in {"leaf", "tool", "loop", "graph", "gate"}
        assert component.row in {1, 2, 3}
        assert component.title
        assert component.summary


def test_catalog_experiment_cases_cover_every_component() -> None:
    exp = build_playground_catalog_experiment()
    # 1 list-endpoint case + 2 per component (detail + cases) + 1
    # 404 case = 1 + 30 + 1 = 32.
    assert len(exp.cases) == 32
    detail_cases = {c.name for c in exp.cases if c.name.startswith("detail_")}
    cases_cases = {c.name for c in exp.cases if c.name.startswith("cases_")}
    for component_id in COMPONENT_IDS:
        assert f"detail_{component_id}" in detail_cases
        assert f"cases_{component_id}" in cases_cases


def test_catalog_experiment_passes_every_case() -> None:
    exp = build_playground_catalog_experiment()
    reports = exp.run_evaluations(task=playground_catalog_task)
    assert len(reports) == 1
    report = reports[0]
    assert all(report.test_passes), [
        (case["name"], reason)
        for case, passed, reason in zip(
            report.cases, report.test_passes, report.reasons
        )
        if not passed
    ]
    threshold, hard_gate = PLAYGROUND_CATALOG_EVALUATOR_THRESHOLDS[report.evaluator_name]
    assert hard_gate is True
    assert report.overall_score >= threshold
