"""Strands-evals experiment for PR 5 — task adapters for c01..c10.

PR 3 wired the ``/playground/components/{id}/run`` endpoint and
populated ``task_attr`` for c11..c15 only. c01..c10 surfaced as
``NO_TASK_ADAPTER``, which was a visible gap rather than a silent
failure.

PR 5 appends a replay ``<name>_task`` to each of the remaining ten
experiment modules and wires them into the registry. This suite
protects that wiring end-to-end by driving the FastAPI
``TestClient`` against each component's first canonical case:

* Deterministic components (c02 timing, c04 audio, c11 assembly …) —
  ``declared_models`` is empty, so the run path must return ``OK``
  plus an ``output`` dict sourced from the case's canonical
  envelope.
* LLM-backed components with any declared models and no provider
  credentials must hard-fail as ``MODEL_UNREACHABLE`` — that is the
  invariant the plan pins ("Not being able to access the model is
  an automatic test failure.")

One :class:`SubsetMatchEvaluator` with a hard-gate threshold of
``1.0`` keeps the shape regression-proof.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from strands_evals.case import Case  # type: ignore[import-not-found]
from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.experiment import Experiment  # type: ignore[import-not-found]

from strands_agents.evals.experiments.playground_catalog import (
    SubsetMatchEvaluator,
)
from strands_agents.playground.reachability import (
    CredentialsProber,
    ReachabilityCache,
    set_default_cache,
)
from strands_agents.playground.registry import get_component


PLAYGROUND_TASKS_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "SubsetMatchEvaluator": (1.0, True),
}


def _client_with_env(env: dict[str, str]) -> TestClient:
    # Late import mirrors ``playground_run.py`` — fresh router view of
    # the module-level reachability cache per case.
    from playground import router as playground_router  # type: ignore[attr-defined]  # noqa: PLC0415

    set_default_cache(ReachabilityCache(CredentialsProber(environ=lambda: env)))
    app = FastAPI()
    app.include_router(playground_router)
    return TestClient(app)


def playground_tasks_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Run the ``case``'s component against its first canonical case."""
    env: dict[str, str] = case.input.get("env", {})
    component_id: str = case.input["component_id"]
    body: dict[str, Any] = case.input.get("body", {})

    client = _client_with_env(env)
    response = client.post(
        f"/playground/components/{component_id}/run", json=body
    )
    return {
        "output": {
            "status_code": response.status_code,
            "body": response.json(),
        }
    }


def _deterministic_case(component_id: str) -> Case[dict[str, Any], dict[str, Any]]:
    """Build a case that expects ``OK`` with the replay envelope.

    Applies to every component whose ``declared_models`` is empty
    (c02, c04, c11) — the reachability gate is a no-op for them, so
    the task adapter is guaranteed to run.
    """
    component = get_component(component_id)
    assert component is not None
    cases = component.cases()
    assert cases, f"{component_id} must have at least one registered case"
    first_name = cases[0].name
    assert first_name, (
        f"{component_id} first case must expose a name after registry "
        "normalisation"
    )
    return Case[dict[str, Any], dict[str, Any]](
        name=f"{component_id}_task_dispatches_ok",
        session_id=f"playground-tasks-{component_id}-ok",
        input={
            "component_id": component_id,
            "env": {},
            "body": {"case_name": first_name},
        },
        expected_output={
            "status_code": 200,
            "body": {
                "status": "OK",
                "component_id": component_id,
                "case_name": first_name,
            },
        },
    )


def _unreachable_case(component_id: str) -> Case[dict[str, Any], dict[str, Any]]:
    """Build a case that expects ``MODEL_UNREACHABLE`` under empty env.

    Applies to every component whose ``declared_models`` is non-empty
    — the reachability probe must hard-fail before the task adapter
    is consulted.
    """
    component = get_component(component_id)
    assert component is not None
    cases = component.cases()
    assert cases, f"{component_id} must have at least one registered case"
    first_name = cases[0].name
    assert first_name
    # The MODEL_UNREACHABLE branch in ``/run`` short-circuits before
    # case resolution, so the envelope carries ``status``,
    # ``component_id`` and ``unreachable_models`` but not
    # ``case_name`` or ``output``. SubsetMatchEvaluator only checks
    # that each key we declare appears with that value, so we pin the
    # subset we care about.
    return Case[dict[str, Any], dict[str, Any]](
        name=f"{component_id}_hard_fails_without_credentials",
        session_id=f"playground-tasks-{component_id}-unreachable",
        input={
            "component_id": component_id,
            "env": {},
            "body": {"case_name": first_name},
        },
        expected_output={
            "status_code": 200,
            "body": {
                "status": "MODEL_UNREACHABLE",
                "component_id": component_id,
                "output": None,
            },
        },
    )


def playground_tasks_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the case corpus covering c01..c10 task-adapter wiring.

    Order mirrors the registry. Deterministic components are expected
    to return ``OK``; LLM-backed components without provider
    credentials must return ``MODEL_UNREACHABLE``.
    """
    deterministic = ["c02", "c04"]
    llm_backed = ["c01", "c03", "c05", "c06", "c07", "c08", "c09", "c10"]
    cases: list[Case[dict[str, Any], dict[str, Any]]] = []
    for cid in deterministic:
        cases.append(_deterministic_case(cid))
    for cid in llm_backed:
        cases.append(_unreachable_case(cid))
    return cases


def playground_tasks_evaluators() -> list[Evaluator[Any, Any]]:
    """Return the evaluator stack — single :class:`SubsetMatchEvaluator`."""
    return [SubsetMatchEvaluator()]


def build_playground_tasks_experiment() -> Experiment[
    dict[str, Any], dict[str, Any]
]:
    """Construct the :class:`Experiment` for the task-adapter suite."""
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=playground_tasks_cases(),
        evaluators=playground_tasks_evaluators(),
    )


__all__ = ["build_playground_tasks_experiment",
    "playground_tasks_cases",
    "playground_tasks_evaluators",
    "playground_tasks_task",]
