"""Strands-evals experiment for the Component Playground catalog.

Exercises the read-only endpoints that land in PR 1 of
``docs/strands-migration/plans/component-playground.md``:

* ``GET /playground/components``
* ``GET /playground/components/{id}``
* ``GET /playground/components/{id}/cases``

The task runs against a :class:`fastapi.testclient.TestClient` that
mounts only the playground router, so the experiment is fast and has
zero external dependencies. Evaluators are deterministic
:class:`Equals` / :class:`Contains` — model reachability is a concern
for PR 2, not for this catalog surface.

Idiomatic shape:

    from strands_agents.evals.experiments.playground_catalog import (
        build_playground_catalog_experiment,
        playground_catalog_task,
    )

    experiment = build_playground_catalog_experiment()
    reports = experiment.run_evaluations(task=playground_catalog_task)
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.experiment import Experiment

from playground import router as playground_router
from strands_agents.playground import iter_components


#: Minimum score per evaluator. The single gate is hard — a broken
#: catalog endpoint should fail the build before any frontend work
#: reaches it.
PLAYGROUND_CATALOG_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "SubsetMatchEvaluator": (1.0, True),
}


def _subset_match(expected: Any, actual: Any) -> tuple[bool, str]:
    """Return whether ``actual`` is a superset of ``expected``.

    * Dicts: every key in ``expected`` must be present in ``actual``
      and subset-match recursively.
    * Lists: ``expected`` entries must all appear (subset-match) in
      ``actual`` at the same index. ``actual`` may contain trailing
      entries ``expected`` did not pin — consistent with the dict arm.
    * Scalars: compared with ``==``.

    Returning the first mismatch's reason keeps failure diagnostics
    terse without importing a heavy diff library.
    """
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False, f"expected dict, got {type(actual).__name__}"
        for key, value in expected.items():
            if key not in actual:
                return False, f"missing key: {key}"
            ok, reason = _subset_match(value, actual[key])
            if not ok:
                return False, f"{key}: {reason}"
        return True, "match"
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False, f"expected list, got {type(actual).__name__}"
        if len(actual) < len(expected):
            return False, f"length {len(actual)} < {len(expected)}"
        for idx, value in enumerate(expected):
            ok, reason = _subset_match(value, actual[idx])
            if not ok:
                return False, f"[{idx}]: {reason}"
        return True, "match"
    if expected == actual:
        return True, "match"
    return False, f"{actual!r} != {expected!r}"


class SubsetMatchEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Deterministic evaluator: actual_output is a superset of expected.

    Strands-evals ships :class:`Equals` which requires the whole
    structure to match. The playground catalog responses contain
    stable anchors (``total``, ``id``, ``component_id``) plus dynamic
    fields (per-case ``session_id`` UUIDs, evaluator metadata), so we
    match only the keys we pinned in ``expected_output``.
    """

    def evaluate(
        self, evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]]
    ) -> list[EvaluationOutput]:
        expected = evaluation_case.expected_output
        actual = evaluation_case.actual_output
        if expected is None:
            return [
                EvaluationOutput(
                    score=1.0, test_pass=True, reason="no expected_output pinned"
                )
            ]
        ok, reason = _subset_match(expected, actual)
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=reason,
            )
        ]

    async def evaluate_async(
        self, evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]]
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


def _build_catalog_app() -> FastAPI:
    """Assemble a minimal FastAPI app containing only the router under test."""
    app = FastAPI(title="playground-catalog-eval")
    app.include_router(playground_router)
    return app


_CLIENT: TestClient = TestClient(_build_catalog_app())


def playground_catalog_task(case: Case[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    """Dispatch a case's HTTP request and return the JSON response.

    Every case carries an ``input`` dict with ``method`` and ``path``.
    The task simply performs the call and returns the parsed body plus
    the HTTP status. Any transport-level error bubbles up as the task
    raising — strands-evals records that as a failed case.
    """
    method = case.input["method"].upper()
    path = case.input["path"]
    response = _CLIENT.request(method, path)
    body: Any
    try:
        body = response.json()
    except ValueError:
        body = response.text
    # Wrap in the strands-evals ``{"output": ...}`` envelope so
    # ``actual_output`` is populated rather than treated as an
    # untagged dict.
    return {"output": {"status": response.status_code, "body": body}}


def _component_count_case() -> Case[dict[str, Any], dict[str, Any]]:
    return Case[dict[str, Any], dict[str, Any]](
        name="components_listed",
        session_id="playground-catalog-001",
        input={"method": "GET", "path": "/playground/components"},
        expected_output={
            "status": 200,
            "body": {"total": 22},
        },
    )


def _component_detail_case(component_id: str, title: str) -> Case[dict[str, Any], dict[str, Any]]:
    return Case[dict[str, Any], dict[str, Any]](
        name=f"detail_{component_id}",
        session_id=f"playground-catalog-detail-{component_id}",
        input={"method": "GET", "path": f"/playground/components/{component_id}"},
        expected_output={
            "status": 200,
            "body": {"id": component_id, "title": title},
        },
    )


def _cases_endpoint_case(component_id: str, case_count: int) -> Case[dict[str, Any], dict[str, Any]]:
    return Case[dict[str, Any], dict[str, Any]](
        name=f"cases_{component_id}",
        session_id=f"playground-catalog-cases-{component_id}",
        input={"method": "GET", "path": f"/playground/components/{component_id}/cases"},
        expected_output={
            "status": 200,
            "body": {"component_id": component_id, "total": case_count},
        },
    )


def _unknown_component_case() -> Case[dict[str, Any], dict[str, Any]]:
    return Case[dict[str, Any], dict[str, Any]](
        name="unknown_component_returns_404",
        session_id="playground-catalog-unknown",
        input={"method": "GET", "path": "/playground/components/c99"},
        expected_output={"status": 404, "body": {"detail": "unknown component: c99"}},
    )


def playground_catalog_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Build the case corpus for the playground catalog.

    One case per structural property we want held:

    * all 15 components are listed;
    * each component's detail endpoint returns the right id + title;
    * each component's cases endpoint reports the expected case count
      (so the catalog and the upstream experiment modules stay in lock-
      step);
    * an unknown component id returns 404 rather than leaking a 500.
    """
    cases: list[Case[dict[str, Any], dict[str, Any]]] = [_component_count_case()]
    for component in iter_components():
        cases.append(_component_detail_case(component.id, component.title))
        cases.append(_cases_endpoint_case(component.id, len(component.cases())))
    cases.append(_unknown_component_case())
    return cases


def playground_catalog_evaluators() -> list[Evaluator[Any, Any]]:
    """Return the deterministic evaluator stack for the catalog surface."""
    return [SubsetMatchEvaluator()]


def build_playground_catalog_experiment() -> Experiment[Any, Any]:
    """Assemble the strands-evals experiment for CI."""
    return Experiment(
        cases=playground_catalog_cases(),
        evaluators=playground_catalog_evaluators(),
    )


__all__ = [
    "PLAYGROUND_CATALOG_EVALUATOR_THRESHOLDS",
    "build_playground_catalog_experiment",
    "playground_catalog_cases",
    "playground_catalog_evaluators",
    "playground_catalog_task",
]
