"""Unit coverage for the playground evaluator endpoint (PR 4).

Locks the endpoint's contract via the Strands experiment so every
asserted case is protected as a unit test, not just inside the eval
harness. Also pins the registry side: every component's evaluator
builder must at least be importable, even if it returns no
evaluators.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from strands_agents.evals.experiments.playground_evaluate import (
    PLAYGROUND_EVALUATE_EVALUATOR_THRESHOLDS,
    build_playground_evaluate_experiment,
    playground_evaluate_cases,
    playground_evaluate_task,
)
from strands_agents.playground.registry import get_component, iter_components


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


class _RaisingSoftEvaluator(Evaluator):
    """Evaluator that always raises — simulates a crashing judge."""

    def evaluate(self, data: EvaluationData[Any, Any]) -> list[EvaluationOutput]:
        raise RuntimeError("simulated evaluator crash")


class _RaisingHardEvaluator(Evaluator):
    """Evaluator that always raises — simulates a crashing hard-gate judge."""

    def evaluate(self, data: EvaluationData[Any, Any]) -> list[EvaluationOutput]:
        raise RuntimeError("simulated hard-gate evaluator crash")


def _build_client() -> TestClient:
    from playground import router as playground_router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(playground_router)
    return TestClient(app)


class _StubComponent:
    """Duck-typed stand-in for ``Component`` used by the endpoint.

    ``Component`` is a frozen dataclass, so we can't monkeypatch its
    instance methods. Instead we build a minimal object that mirrors
    the attributes the ``/evaluate`` handler reads, and swap in
    ``get_component`` via monkeypatch.
    """

    def __init__(
        self,
        component_id: str,
        cases: list[Any],
        evaluator_instances: list[Evaluator],
        evaluator_decls: list[Any],
    ) -> None:
        self.id = component_id
        self._cases = cases
        self._evaluator_instances = evaluator_instances
        self._evaluator_decls = evaluator_decls

    def cases(self) -> list[Any]:
        return self._cases

    def evaluator_instances(self) -> list[Evaluator]:
        return self._evaluator_instances

    def evaluators(self) -> list[Any]:
        return self._evaluator_decls


def test_soft_evaluator_crash_does_not_fail_overall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-hard-gate evaluator that raises must not fail ``overall_passed``.

    Matches the non-exception branch's semantics: a soft evaluator
    scoring ``0.0`` (clearly failing) does not trip ``overall_passed``,
    so a soft evaluator that *crashes* must not either. Only a
    crashing hard-gate evaluator trips the overall gate — asserted
    separately in :func:`test_hard_evaluator_crash_fails_overall`.
    """
    import playground as playground_module  # noqa: PLC0415

    real = get_component("c15")
    assert real is not None
    stub = _StubComponent(
        component_id="c15",
        cases=list(real.cases()),
        evaluator_instances=[_RaisingSoftEvaluator()],
        evaluator_decls=[],  # soft path: no declaration → hard_gate=False
    )
    monkeypatch.setattr(
        playground_module,
        "get_component",
        lambda cid: stub if cid == "c15" else None,
    )

    client = _build_client()
    response = client.post(
        "/playground/components/c15/evaluate",
        json={
            "case_name": stub.cases()[0].name,
            "actual_output": {"anything": "goes"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "OK"
    assert payload["overall_passed"] is True, (
        "soft evaluator crash must not fail the overall assessment — "
        f"payload={payload}"
    )
    assert len(payload["results"]) == 1
    row = payload["results"][0]
    assert row["status"] == "EVALUATOR_ERROR"
    assert row["passed"] is False
    assert row["hard_gate"] is False
    assert "simulated evaluator crash" in row["error"]


def test_hard_evaluator_crash_fails_overall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hard-gate evaluator that raises must fail ``overall_passed``.

    Pairs with :func:`test_soft_evaluator_crash_does_not_fail_overall`
    — together they pin the gate semantics for both exception and
    non-exception branches.
    """
    from strands_agents.playground.registry import EvaluatorDeclaration
    import playground as playground_module  # noqa: PLC0415

    real = get_component("c15")
    assert real is not None
    stub = _StubComponent(
        component_id="c15",
        cases=list(real.cases()),
        evaluator_instances=[_RaisingHardEvaluator()],
        evaluator_decls=[
            EvaluatorDeclaration(
                name="_RaisingHardEvaluator", threshold=1.0, hard_gate=True
            )
        ],
    )
    monkeypatch.setattr(
        playground_module,
        "get_component",
        lambda cid: stub if cid == "c15" else None,
    )

    client = _build_client()
    response = client.post(
        "/playground/components/c15/evaluate",
        json={
            "case_name": stub.cases()[0].name,
            "actual_output": {"anything": "goes"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "OK"
    assert payload["overall_passed"] is False
    assert len(payload["results"]) == 1
    row = payload["results"][0]
    assert row["status"] == "EVALUATOR_ERROR"
    assert row["passed"] is False
    assert row["hard_gate"] is True
