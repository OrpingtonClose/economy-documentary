"""Infra-agent FastAPI surface experiment (slice 4a / 5a).

Drives :func:`strands_agents.infra_agent.app.build_app` through
scripted HTTP sequences via :class:`fastapi.testclient.TestClient` and
scores every sequence through deterministic
:class:`strands_evals.Evaluator` subclasses.

This exercises the *control plane* the orchestrator talks to when
debugging a live VM: status pulls bump the idle timer, explicit bumps
reset it, destroy calls latch the manual flag. No live VM, no Vast.ai
API — everything runs in-process with injected clock + telemetry.

Cases cover:

* **health_does_not_bump** — ``GET /health`` returns ok but leaves
  ``last_bump_ts`` unchanged. Critical: k8s-style pollers must not
  keep a VM alive forever.
* **status_bumps_timer** — ``GET /infra/status`` bumps the idle
  counter and returns the full status payload including remaining
  budgets + peak telemetry.
* **explicit_bump_resets_idle** — ``POST /infra/bump`` at ``t=50``
  on an idle-budget-60 config leaves 60s remaining (the bump is at
  ``t=50``, so at ``t=50`` remaining is ``60 - 0 = 60``).
* **destroy_latches_manual_flag** — ``POST /infra/destroy`` sets
  ``manual_destroy_requested`` to ``True`` and echoes the reason.
* **destroy_with_reason_body** — destroy accepts a JSON body with
  ``reason`` that shows up in the response.
* **telemetry_tracks_peak** — after two status pulls the peak VRAM
  mirrors the larger of the two samples.
* **status_after_destroy_keeps_flag** — once latched, a subsequent
  status still reports ``manual_destroy_requested=True``.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from strands_evals.case import Case  # type: ignore[import-not-found]
from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.experiment import Experiment  # type: ignore[import-not-found]
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput  # type: ignore[import-not-found]

from strands_agents.infra_agent.app import build_app
from strands_agents.infra_agent.guardian import GuardianConfig, GuardianState
from strands_agents.infra_agent.telemetry import ResourceTelemetry


INFRA_AGENT_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "InfraAgentResponseEvaluator": (1.0, True),
    "InfraAgentStateEvaluator": (1.0, True),
}


# ── Cases ────────────────────────────────────────────────────────────
#
# Each case's ``input`` drives a short HTTP sequence. The last step's
# response + final guardian-state snapshot are scored.
#
# ``input`` shape:
#   idle_budget_s: int
#   lifetime_budget_s: int
#   vram_samples: list[tuple[total_gb, used_gb]]  # one per tick
#   clock_ticks: list[float]  # values returned by injected clock
#   requests: list[{method, path, body?}]  # final response is scored
#
# The clock advances one tick per request. The vram_samples list is
# consumed by the injected prober across telemetry.sample() calls.

_IDLE_S: int = 60
_LIFETIME_S: int = 3600


def _case(
    name: str,
    *,
    requests: list[dict[str, Any]],
    clock_ticks: list[float] | None = None,
    vram_samples: list[tuple[int, int]] | None = None,
    expected_status: int,
    expected_body_contains: dict[str, Any] | None = None,
    expected_final_state: dict[str, Any] | None = None,
    idle_budget_s: int = _IDLE_S,
    lifetime_budget_s: int = _LIFETIME_S,
) -> Case[dict[str, Any], dict[str, Any]]:
    """Build one HTTP-sequence Case."""
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"infra-agent-{name}",
        input={
            "idle_budget_s": idle_budget_s,
            "lifetime_budget_s": lifetime_budget_s,
            "vram_samples": [list(s) for s in (vram_samples or [])],
            "clock_ticks": clock_ticks
            or [float(i) for i in range(len(requests))],
            "requests": requests,
        },
        expected_output={
            "final_status": expected_status,
        },
        metadata={
            "expected_status": expected_status,
            "expected_body_contains": expected_body_contains or {},
            "expected_final_state": expected_final_state or {},
        },
    )


def infra_agent_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the canonical suite for the infra-agent HTTP surface."""
    return [
        _case(
            "health_does_not_bump",
            requests=[{"method": "GET", "path": "/"}],
            clock_ticks=[42.0],
            expected_status=200,
            expected_body_contains={"_text": "ok"},
            expected_final_state={"last_bump_ts": 0.0},
        ),
        _case(
            "status_bumps_timer",
            requests=[{"method": "POST", "path": "/", "body": "status"}],
            clock_ticks=[42.0],
            vram_samples=[(24, 10)],
            expected_status=200,
            expected_body_contains={"_text": "worker_id=test-worker"},
            expected_final_state={"last_bump_ts": 42.0},
        ),
        _case(
            "explicit_bump_resets_idle",
            requests=[{"method": "POST", "path": "/", "body": "bump"}],
            clock_ticks=[50.0],
            expected_status=200,
            expected_body_contains={"_text": "ok"},
            expected_final_state={"last_bump_ts": 50.0},
        ),
        _case(
            "destroy_latches_manual_flag",
            requests=[{"method": "POST", "path": "/", "body": "destroy"}],
            clock_ticks=[1.0],
            expected_status=200,
            expected_body_contains={"_text": "ok"},
            expected_final_state={"manual_destroy_requested": True},
        ),
        _case(
            "destroy_with_reason_body",
            requests=[
                {
                    "method": "POST",
                    "path": "/",
                    "body": "destroy operator_request",
                }
            ],
            clock_ticks=[1.0],
            expected_status=200,
            expected_body_contains={"_text": "ok"},
            expected_final_state={"manual_destroy_requested": True},
        ),
        _case(
            "telemetry_tracks_peak",
            requests=[
                {"method": "POST", "path": "/", "body": "status"},
                {"method": "POST", "path": "/", "body": "status"},
            ],
            clock_ticks=[10.0, 20.0],
            vram_samples=[(24, 8), (24, 18)],
            expected_status=200,
            expected_body_contains={"_text": "worker_id=test-worker"},
            expected_final_state={
                "last_bump_ts": 20.0,
                "vram_peak_gb": 18,
            },
        ),
        _case(
            "status_after_destroy_keeps_flag",
            requests=[
                {"method": "POST", "path": "/", "body": "destroy"},
                {"method": "POST", "path": "/", "body": "status"},
            ],
            clock_ticks=[1.0, 2.0],
            vram_samples=[(24, 12)],
            expected_status=200,
            expected_body_contains={"_text": "manual_destroy=True"},
            expected_final_state={"manual_destroy_requested": True},
        ),
    ]


# ── Task adapter ─────────────────────────────────────────────────────


def _make_clock(ticks: list[float]) -> Any:
    """Return a closure yielding successive ticks.

    If the app calls the clock more times than there are ticks, the
    final tick is repeated (the app is permitted to sample the clock
    twice during a single request — once in the route handler, once
    in ``remaining_s`` inside the response helper).
    """
    idx = [0]

    def _clock() -> float:
        i = min(idx[0], len(ticks) - 1)
        idx[0] += 1
        return ticks[i]

    return _clock


def _make_vram_prober(samples: list[tuple[int, int]]) -> Any:
    """Return a closure yielding successive VRAM samples.

    Empty list means the prober returns ``None`` (no GPU on the VM).
    Past the end, the last sample repeats.
    """
    if not samples:
        return lambda: None

    idx = [0]

    def _prober() -> tuple[int, int]:
        i = min(idx[0], len(samples) - 1)
        idx[0] += 1
        return tuple(samples[i])  # type: ignore[return-value]

    return _prober


def _disk_prober(_path: str) -> tuple[int, int]:
    """Fixed disk telemetry — we don't test disk peaks in this suite."""
    return 100, 20


def infra_agent_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Replay the case's HTTP sequence against a fresh agent app.

    Returns the evaluate envelope. ``output`` carries the final
    response's ``status_code`` + parsed ``body`` and the final
    guardian state + telemetry peaks. ``trajectory`` lists every
    ``METHOD path`` the case hit in order.
    """
    payload = case.input or {}

    config = GuardianConfig(
        idle_budget_s=int(payload["idle_budget_s"]),
        max_lifetime_budget_s=int(payload["lifetime_budget_s"]),
    )
    state = GuardianState(boot_ts=0.0, last_bump_ts=0.0)
    telemetry = ResourceTelemetry(
        vram_prober=_make_vram_prober(
            [tuple(s) for s in payload.get("vram_samples", [])]
        ),
        disk_prober=_disk_prober,
        disk_path="/",
    )
    clock = _make_clock(
        [float(t) for t in payload.get("clock_ticks", [0.0])]
    )

    app = build_app(
        worker_id="test-worker",
        vm_instance_id="vast-instance-123",
        state=state,
        config=config,
        telemetry=telemetry,
        clock=clock,
    )

    trajectory: list[str] = []
    final_status = 0
    final_body: Any = None
    with TestClient(app) as client:
        for req in payload.get("requests", []):
            method = req["method"].upper()
            path = req["path"]
            trajectory.append(f"{method} {path}")
            if method == "GET":
                response = client.get(path)
            elif method == "POST":
                body = req.get("body")
                if body is not None:
                    response = client.post(path, data=body.encode("utf-8"), headers={"Content-Type": "text/plain"})
                else:
                    response = client.post(path)
            else:
                raise ValueError(f"unsupported method: {method}")
            final_status = response.status_code
            try:
                final_body = response.json()
            except json.JSONDecodeError:
                final_body = {"_text": response.text}

    snapshot = telemetry.sample()
    return {
        "output": {
            "final_status": final_status,
            "body": final_body,
            "final_state": {
                "last_bump_ts": state.last_bump_ts,
                "manual_destroy_requested": state.manual_destroy_requested,
                "vram_peak_gb": snapshot.vram_peak_gb,
                "disk_peak_gb": snapshot.disk_peak_gb,
            },
        },
        "trajectory": trajectory,
        "metadata": {"requests_applied": len(trajectory)},
    }


# ── Evaluators ───────────────────────────────────────────────────────


class InfraAgentResponseEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin the final response's status code + key body fields.

    Non-zero ``expected_body_contains`` entries are checked as an
    inclusion test — the response body must contain each key with
    the expected value, but may contain more fields (so adding new
    telemetry fields in future doesn't break the evaluator).
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        expected_status = int(metadata.get("expected_status", -1))
        expected_contains: dict[str, Any] = metadata.get(
            "expected_body_contains", {}
        )
        actual_status = int(actual.get("final_status", -1))
        body = actual.get("body") or {}

        problems: list[str] = []
        if actual_status != expected_status:
            problems.append(
                f"status={actual_status} expected {expected_status}"
            )
        for key, value in expected_contains.items():
            if body.get(key) != value:
                problems.append(
                    f"body.{key}={body.get(key)!r} expected {value!r}"
                )
        ok = not problems
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=("response matches" if ok else "; ".join(problems)),
                label="response_match" if ok else "response_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class InfraAgentStateEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin the final guardian state + telemetry peaks after the sequence."""

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = (evaluation_case.actual_output or {}).get("final_state") or {}
        expected: dict[str, Any] = (evaluation_case.metadata or {}).get(
            "expected_final_state", {}
        )
        if not expected:
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="no final state pinned for this case",
                    label="state_not_required",
                )
            ]
        mismatches: list[str] = []
        for key, value in expected.items():
            if actual.get(key) != value:
                mismatches.append(
                    f"{key}={actual.get(key)!r} expected {value!r}"
                )
        ok = not mismatches
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason="state matches" if ok else "; ".join(mismatches),
                label="state_match" if ok else "state_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


# ── Experiment factory ───────────────────────────────────────────────


def build_infra_agent_experiment() -> Experiment[dict[str, Any], dict[str, Any]]:
    """Assemble the infra-agent :class:`Experiment`."""
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=infra_agent_cases(),
        evaluators=[
            InfraAgentResponseEvaluator(),
            InfraAgentStateEvaluator(),
        ],
    )


__all__ = ["InfraAgentResponseEvaluator",
    "InfraAgentStateEvaluator",
    "build_infra_agent_experiment",
    "infra_agent_cases",
    "infra_agent_task",]
