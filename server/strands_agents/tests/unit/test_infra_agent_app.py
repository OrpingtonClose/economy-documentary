"""Unit tests for the infra agent FastAPI surface."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from strands_agents.infra_agent.app import build_app
from strands_agents.infra_agent.guardian import GuardianConfig, GuardianState
from strands_agents.infra_agent.telemetry import ResourceTelemetry


class _Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def setup() -> dict[str, Any]:
    clock = _Clock(start=1_000.0)
    state = GuardianState(boot_ts=1_000.0, last_bump_ts=1_000.0)
    config = GuardianConfig(idle_budget_s=60, max_lifetime_budget_s=3_600)
    telemetry = ResourceTelemetry(
        vram_prober=lambda: (80, 20),
        disk_prober=lambda _path: (500, 40),
        disk_path="/",
    )
    app = build_app(
        worker_id="tts-alpha",
        vm_instance_id="vast-42",
        state=state,
        config=config,
        telemetry=telemetry,
        clock=clock,
    )
    return {
        "client": TestClient(app),
        "clock": clock,
        "state": state,
        "config": config,
        "telemetry": telemetry,
    }


def test_health_does_not_bump(setup: dict[str, Any]) -> None:
    client = setup["client"]
    clock = setup["clock"]
    state = setup["state"]

    clock.advance(30.0)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "worker_id": "tts-alpha"}
    assert state.last_bump_ts == 1_000.0  # unchanged


def test_status_payload_shape(setup: dict[str, Any]) -> None:
    client = setup["client"]
    clock = setup["clock"]
    clock.advance(15.0)

    response = client.get("/infra/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["worker_id"] == "tts-alpha"
    assert payload["vm_instance_id"] == "vast-42"
    assert payload["boot_ts"] == 1_000.0
    assert payload["uptime_s"] == pytest.approx(15.0)
    assert payload["idle_budget_s"] == 60
    assert payload["lifetime_budget_s"] == 3_600
    assert payload["manual_destroy_requested"] is False
    assert payload["telemetry"]["vram_total_gb"] == 80
    assert payload["telemetry"]["vram_used_gb"] == 20
    assert payload["telemetry"]["vram_peak_gb"] == 20
    assert payload["telemetry"]["disk_peak_gb"] == 40


def test_status_resets_idle_timer(setup: dict[str, Any]) -> None:
    client = setup["client"]
    clock = setup["clock"]
    state = setup["state"]

    clock.advance(30.0)
    response = client.get("/infra/status")
    payload = response.json()

    assert state.last_bump_ts == 1_030.0
    assert payload["idle_remaining_s"] == pytest.approx(60.0)


def test_bump_resets_idle_timer(setup: dict[str, Any]) -> None:
    client = setup["client"]
    clock = setup["clock"]
    state = setup["state"]

    clock.advance(45.0)
    response = client.post("/infra/bump")

    assert response.status_code == 200
    assert state.last_bump_ts == 1_045.0
    assert response.json()["idle_remaining_s"] == pytest.approx(60.0)


def test_destroy_latches_manual_flag(setup: dict[str, Any]) -> None:
    client = setup["client"]
    state = setup["state"]

    response = client.post("/infra/destroy", json={"reason": "slice-4b smoke"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["manual_destroy_requested"] is True
    assert payload["reason"] == "slice-4b smoke"
    assert state.manual_destroy_requested is True


def test_destroy_accepts_empty_body(setup: dict[str, Any]) -> None:
    client = setup["client"]

    response = client.post("/infra/destroy")

    assert response.status_code == 200
    assert response.json()["reason"] == "manual"


def test_status_telemetry_tracks_peak_across_calls() -> None:
    clock = _Clock()
    state = GuardianState(boot_ts=1_000.0, last_bump_ts=1_000.0)
    config = GuardianConfig(idle_budget_s=60, max_lifetime_budget_s=3_600)
    readings = iter([(80, 10), (80, 55), (80, 20)])
    telemetry = ResourceTelemetry(
        vram_prober=lambda: next(readings),
        disk_prober=lambda _path: (500, 30),
    )
    app = build_app(
        worker_id="w",
        vm_instance_id=None,
        state=state,
        config=config,
        telemetry=telemetry,
        clock=clock,
    )
    client = TestClient(app)

    assert client.get("/infra/status").json()["telemetry"]["vram_peak_gb"] == 10
    assert client.get("/infra/status").json()["telemetry"]["vram_peak_gb"] == 55
    assert client.get("/infra/status").json()["telemetry"]["vram_peak_gb"] == 55
