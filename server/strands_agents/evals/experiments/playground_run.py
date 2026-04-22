"""Strands-evals experiment for the run endpoint (PR 3).

Protects the run endpoint's four-state contract:

* ``OK`` — model reachable, case resolved, task dispatched, envelope
  returned.
* ``MODEL_UNREACHABLE`` — any declared model unreachable short-
  circuits before the task runs. This is the hard-gate the plan
  pins.
* ``NO_TASK_ADAPTER`` — the upstream experiment module does not
  export the declared ``task_attr``. Visible gap, not a silent
  fallback.
* ``TASK_ERROR`` — the task raised. Surfaced for debugging.

The suite drives the FastAPI ``TestClient`` so the HTTP envelope is
exercised end-to-end, not just the underlying registry helpers.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment

from strands_agents.evals.experiments.playground_catalog import (
    SubsetMatchEvaluator,
)
from strands_agents.playground.reachability import (
    CredentialsProber,
    ReachabilityCache,
    set_default_cache,
)


PLAYGROUND_RUN_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "SubsetMatchEvaluator": (1.0, True),
}


def _client_with_env(env: dict[str, str]) -> TestClient:
    # Import late so every case gets a fresh router view of the
    # module-level reachability cache.
    from playground import router as playground_router  # noqa: PLC0415

    set_default_cache(ReachabilityCache(CredentialsProber(environ=lambda: env)))
    app = FastAPI()
    app.include_router(playground_router)
    return TestClient(app)


def playground_run_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Dispatch a case against the run endpoint and return the body."""
    scenario = case.input["scenario"]
    env: dict[str, str] = case.input.get("env", {})
    component_id: str = case.input["component_id"]
    body: dict[str, Any] = case.input.get("body", {})

    client = _client_with_env(env)

    if scenario == "run":
        response = client.post(
            f"/playground/components/{component_id}/run", json=body
        )
        return {"output": {"status_code": response.status_code, "body": response.json()}}

    if scenario == "schema":
        response = client.get(f"/playground/components/{component_id}/schema")
        return {"output": {"status_code": response.status_code, "body": response.json()}}

    raise ValueError(f"unknown scenario: {scenario}")


def _run_case(
    name: str,
    *,
    component_id: str,
    body: dict[str, Any],
    env: dict[str, str],
    expected: dict[str, Any],
) -> Case[dict[str, Any], dict[str, Any]]:
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"playground-run-{name}",
        input={
            "scenario": "run",
            "component_id": component_id,
            "body": body,
            "env": env,
        },
        expected_output=expected,
    )


#: Environment that satisfies every provider the 15 components
#: declare, so the reachability gate passes and the run path runs.
_FULL_ENV: dict[str, str] = {
    "GEMINI_API_KEY": "sk-test",
    "OPENAI_API_KEY": "sk-test",
    "KIMI_API_KEY": "sk-test",
    "OLLAMA_HOST": "http://localhost:11434",
}


def playground_run_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Build the case corpus for PR 3."""
    return [
        # Deterministic run: c15 (approval) declares no models and
        # its ``approval_task`` returns a canonical trajectory from
        # each case's metadata, so the run path completes without
        # any LLM in the loop.
        _run_case(
            "deterministic_component_runs_ok",
            component_id="c15",
            body={"case_name": "accept_visual_dispatch"},
            env={},
            expected={
                "status_code": 200,
                "body": {"status": "OK", "component_id": "c15"},
            },
        ),
        # Custom input, same deterministic component.
        _run_case(
            "custom_input_runs_ok",
            component_id="c15",
            body={"custom_input": {"foo": "bar"}, "case_name": "my_case"},
            env={},
            expected={
                "status_code": 200,
                "body": {
                    "status": "OK",
                    "component_id": "c15",
                    "case_name": "my_case",
                },
            },
        ),
        # LLM component, empty env → MODEL_UNREACHABLE. Hard gate.
        _run_case(
            "llm_component_without_credentials_hard_fails",
            component_id="c01",
            body={"case_name": "anything"},
            env={},
            expected={
                "status_code": 200,
                "body": {
                    "status": "MODEL_UNREACHABLE",
                    "component_id": "c01",
                },
            },
        ),
        # LLM component, full env → passes reachability; c01 has no
        # task_attr → NO_TASK_ADAPTER (visible gap).
        _run_case(
            "reachable_but_no_task_adapter",
            component_id="c01",
            body={"case_name": "economics_basics"},
            env=_FULL_ENV,
            expected={
                "status_code": 200,
                "body": {
                    "status": "NO_TASK_ADAPTER",
                    "component_id": "c01",
                },
            },
        ),
        # Unknown component → 404.
        _run_case(
            "unknown_component_returns_404",
            component_id="c99",
            body={"case_name": "anything"},
            env={},
            expected={"status_code": 404},
        ),
        # Neither case_name nor custom_input → 400.
        _run_case(
            "missing_case_and_custom_returns_400",
            component_id="c15",
            body={},
            env={},
            expected={"status_code": 400},
        ),
        # Unknown case_name on a known component → 400.
        _run_case(
            "unknown_case_name_returns_400",
            component_id="c15",
            body={"case_name": "never_registered"},
            env={},
            expected={"status_code": 400},
        ),
        # Schema endpoint returns fields + sample_input.
        Case[dict[str, Any], dict[str, Any]](
            name="schema_endpoint_returns_fields",
            session_id="playground-run-schema-c15",
            input={
                "scenario": "schema",
                "component_id": "c15",
                "env": {},
            },
            expected_output={
                "status_code": 200,
                "body": {"component_id": "c15"},
            },
        ),
        Case[dict[str, Any], dict[str, Any]](
            name="schema_endpoint_404_for_unknown",
            session_id="playground-run-schema-c99",
            input={
                "scenario": "schema",
                "component_id": "c99",
                "env": {},
            },
            expected_output={"status_code": 404},
        ),
    ]


def playground_run_evaluators() -> list[Evaluator[Any, Any]]:
    return [SubsetMatchEvaluator()]


def build_playground_run_experiment() -> Experiment[Any, Any]:
    return Experiment(
        cases=playground_run_cases(),
        evaluators=playground_run_evaluators(),
    )


__all__ = [
    "PLAYGROUND_RUN_EVALUATOR_THRESHOLDS",
    "build_playground_run_experiment",
    "playground_run_cases",
    "playground_run_evaluators",
    "playground_run_task",
]
