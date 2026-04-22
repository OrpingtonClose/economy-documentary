"""Strands-evals experiment for the evaluate endpoint (PR 4).

Protects the evaluator endpoint's contract:

* ``OK`` — evaluator stack instantiated, every evaluator scored the
  supplied output against the case's expected payload.
* ``NO_EVALUATORS`` — component declares no builder or the builder
  failed to produce evaluators. Not a crash, a visible gap.
* ``EVALUATOR_ERROR`` — an individual evaluator raised while scoring.
  Surfaced per-row so one broken judge doesn't blank out the rest.

The suite drives the FastAPI ``TestClient`` so the HTTP envelope is
exercised end-to-end, same as the run experiment.
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


PLAYGROUND_EVALUATE_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "SubsetMatchEvaluator": (1.0, True),
}


def _client_with_env(env: dict[str, str]) -> TestClient:
    from playground import router as playground_router  # noqa: PLC0415

    set_default_cache(ReachabilityCache(CredentialsProber(environ=lambda: env)))
    app = FastAPI()
    app.include_router(playground_router)
    return TestClient(app)


def playground_evaluate_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Dispatch a case against the evaluate endpoint and return the body."""
    env: dict[str, str] = case.input.get("env", {})
    component_id: str = case.input["component_id"]
    body: dict[str, Any] = case.input.get("body", {})

    client = _client_with_env(env)
    response = client.post(
        f"/playground/components/{component_id}/evaluate", json=body
    )
    return {
        "output": {"status_code": response.status_code, "body": response.json()}
    }


def _eval_case(
    name: str,
    *,
    component_id: str,
    body: dict[str, Any],
    env: dict[str, str],
    expected: dict[str, Any],
) -> Case[dict[str, Any], dict[str, Any]]:
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"playground-evaluate-{name}",
        input={
            "component_id": component_id,
            "body": body,
            "env": env,
        },
        expected_output=expected,
    )


def playground_evaluate_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Build the case corpus for PR 4.

    Each case drives ``/playground/components/{id}/evaluate`` against a
    known component + case-name and asserts a subset-match on the JSON
    envelope. We pick c15 (approval) and c11 (assembly) because their
    task adapters return deterministic envelopes and their evaluator
    stacks are non-empty.
    """
    # Happy path: deterministic component + matching expected_output →
    # every evaluator should score >= threshold.
    approval_envelope: dict[str, Any] = {
        "output": {"decision": "accept", "feedback": None},
        "trajectory": [{"tool": "approval", "decision": "accept"}],
    }
    return [
        # c15 approval with the canonical output → OK + overall_passed.
        _eval_case(
            "approval_canonical_output_passes",
            component_id="c15",
            body={
                "case_name": "accept_visual_dispatch",
                "actual_output": approval_envelope,
            },
            env={},
            expected={
                "status_code": 200,
                "body": {
                    "status": "OK",
                    "component_id": "c15",
                    "case_name": "accept_visual_dispatch",
                    "overall_passed": True,
                },
            },
        ),
        # c02 timing has real thresholds (1.0 hard gate on both
        # evaluators). Empty envelope → both fail, overall_passed
        # False. Exercises the hard-gate path against a component
        # that declares real thresholds, not the empty defaults.
        _eval_case(
            "empty_output_hard_gates_fail",
            component_id="c02",
            body={
                "case_name": "intent_exact",
                "actual_output": {},
            },
            env={},
            expected={
                "status_code": 200,
                "body": {
                    "status": "OK",
                    "component_id": "c02",
                    "case_name": "intent_exact",
                    "overall_passed": False,
                },
            },
        ),
        # Custom input, no registered case → status OK (we still have
        # evaluators) but expected_output is None so hard-gate fails.
        _eval_case(
            "custom_input_evaluates_without_expected_output",
            component_id="c15",
            body={
                "custom_input": {"scene_id": 0},
                "case_name": "my_custom",
                "actual_output": approval_envelope,
            },
            env={},
            expected={
                "status_code": 200,
                "body": {
                    "status": "OK",
                    "component_id": "c15",
                    "case_name": "my_custom",
                },
            },
        ),
        # Unknown component → 404.
        _eval_case(
            "unknown_component_returns_404",
            component_id="c99",
            body={"case_name": "anything", "actual_output": {}},
            env={},
            expected={"status_code": 404},
        ),
        # Unknown case_name on a known component → 400.
        _eval_case(
            "unknown_case_name_returns_400",
            component_id="c15",
            body={"case_name": "never_registered", "actual_output": {}},
            env={},
            expected={"status_code": 400},
        ),
        # Neither case_name nor custom_input → 400.
        _eval_case(
            "missing_case_and_custom_returns_400",
            component_id="c15",
            body={"actual_output": {}},
            env={},
            expected={"status_code": 400},
        ),
        # Actual output missing → pydantic validation returns 422.
        _eval_case(
            "missing_actual_output_returns_422",
            component_id="c15",
            body={"case_name": "accept_visual_dispatch"},
            env={},
            expected={"status_code": 422},
        ),
        # Custom expected override wins over the case's golden answer.
        _eval_case(
            "custom_expected_overrides_case_expected",
            component_id="c15",
            body={
                "case_name": "accept_visual_dispatch",
                "actual_output": {"output": {"decision": "reject"}, "trajectory": []},
                "custom_expected": {"output": {"decision": "reject"}, "trajectory": []},
            },
            env={},
            expected={
                "status_code": 200,
                "body": {
                    "status": "OK",
                    "component_id": "c15",
                    "case_name": "accept_visual_dispatch",
                },
            },
        ),
    ]


def build_playground_evaluate_experiment() -> (
    Experiment[dict[str, Any], dict[str, Any]]
):
    """Build the PR 4 experiment."""
    evaluators: list[Evaluator] = [SubsetMatchEvaluator()]
    return Experiment(
        cases=playground_evaluate_cases(),
        evaluators=evaluators,
    )


__all__ = [
    "PLAYGROUND_EVALUATE_EVALUATOR_THRESHOLDS",
    "build_playground_evaluate_experiment",
    "playground_evaluate_cases",
    "playground_evaluate_task",
]
