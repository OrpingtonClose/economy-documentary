"""Unit tests for the playground run endpoint (PR 3).

Covers the four run statuses (``OK``, ``MODEL_UNREACHABLE``,
``NO_TASK_ADAPTER``, ``TASK_ERROR``) and the schema endpoint via the
strands-evals experiment runner — no pytest-driven assertions inside
the component, just the experiment harness.
"""

from __future__ import annotations

from typing import Any

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


def test_run_endpoint_does_not_short_circuit_on_partial_reachability() -> None:
    """Regression guard: a single unreachable declared model must not
    block a run when another declared model is reachable.

    Previously the run endpoint gated on *every* declared model being
    reachable, so an expired Kimi key on the staging VM (where Gemini
    and GPT-4o were both green) produced MODEL_UNREACHABLE for every
    c01 run. The user policy is: the model is part of the spec,
    unreachable models are discard candidates surfaced on the catalog
    — but a run only needs ONE reachable model to proceed.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from playground import router as playground_router
    from strands_agents.playground.reachability import (
        DeclaredModel,
        ModelProber,
        ReachabilityCache,
        ReachabilityStatus,
        set_default_cache,
    )

    from strands_agents.playground import get_component

    class PartialProber:
        """Report only models whose id contains ``openai/`` as reachable."""

        def probe(self, model: DeclaredModel) -> ReachabilityStatus:
            reachable = "openai/" in model.id
            return ReachabilityStatus(
                model_id=model.id,
                provider=model.provider,
                reachable=reachable,
                reason="ok" if reachable else "probe_error:AuthError",
                checked_at=0.0,
                latency_ms=0.0,
            )

    # Install the synthetic cache so c01's declared models probe
    # green for OpenAI and red for the other two. Also stub c01's task
    # adapter — the point of this test is the partial-reachability
    # short-circuit behaviour, not the live scenario agent, which would
    # call out to litellm and hang in CI.
    prober: ModelProber = PartialProber()
    component = get_component("c01")
    assert component is not None
    sentinel: dict[str, Any] = {"output": {"scenes": []}, "trajectory": []}
    previous_task = component._cache.get("task")
    component._cache["task"] = lambda _case: sentinel
    previous_cache = set_default_cache(ReachabilityCache(prober))
    try:
        app = FastAPI()
        app.include_router(playground_router)
        client = TestClient(app)
        response = client.post(
            "/playground/components/c01/run",
            json={"case_name": "economics_basics"},
        )
        assert response.status_code == 200
        body = response.json()
        # The stubbed task returns the sentinel, so we expect OK — but
        # the assertion that matters is that it is NOT
        # MODEL_UNREACHABLE: partial reachability did not short-circuit
        # the dispatch.
        assert body["status"] != "MODEL_UNREACHABLE", body
        assert body["status"] == "OK", body
        assert body["output"] == sentinel["output"]
    finally:
        set_default_cache(previous_cache)
        if previous_task is None:
            component._cache.pop("task", None)
        else:
            component._cache["task"] = previous_task


def test_run_endpoint_returns_model_unreachable_when_nothing_reachable() -> None:
    """Guard the other side: if *no* declared model is reachable,
    the run endpoint must still short-circuit with MODEL_UNREACHABLE.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from playground import router as playground_router
    from strands_agents.playground.reachability import (
        DeclaredModel,
        ReachabilityCache,
        ReachabilityStatus,
        set_default_cache,
    )

    class AlwaysUnreachable:
        def probe(self, model: DeclaredModel) -> ReachabilityStatus:
            return ReachabilityStatus(
                model_id=model.id,
                provider=model.provider,
                reachable=False,
                reason="probe_error:AuthError",
                checked_at=0.0,
                latency_ms=0.0,
            )

    previous_cache = set_default_cache(ReachabilityCache(AlwaysUnreachable()))
    try:
        app = FastAPI()
        app.include_router(playground_router)
        client = TestClient(app)

        response = client.post(
            "/playground/components/c01/run",
            json={"case_name": "economics_basics"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "MODEL_UNREACHABLE", body
        assert body["component_id"] == "c01"
        assert len(body["unreachable_models"]) >= 1
    finally:
        set_default_cache(previous_cache)


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
