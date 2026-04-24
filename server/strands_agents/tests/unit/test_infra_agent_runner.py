"""Unit tests for the infra agent runner helpers."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from strands_agents.infra_agent.guardian import (
    DestroyReason,
    GuardianConfig,
    GuardianState,
)
from strands_agents.infra_agent.registry_client import (
    PlaygroundRegistryClient,
    RegistryDeregisterError,
    _HttpResponse,
)
from strands_agents.infra_agent.runner import (
    decision_tick_loop,
    run_destroy_sequence,
)
from strands_agents.infra_agent.telemetry import ResourceTelemetry
from strands_agents.infra_agent.vast_client import VastAiClient, VastAiDestroyError


def _make_telemetry() -> ResourceTelemetry:
    return ResourceTelemetry(
        vram_prober=lambda: (80, 10),
        disk_prober=lambda _path: (500, 30),
    )


def test_destroy_sequence_deregisters_then_destroys() -> None:
    deregister_calls: list[str] = []
    destroy_calls: list[str] = []

    def fake_deregister(*, url: str, timeout: float) -> _HttpResponse:
        deregister_calls.append(url)
        return _HttpResponse(status_code=204, text="")

    def fake_vast_delete(*, url: str, headers: dict[str, str], timeout: float) -> object:
        destroy_calls.append(url)
        return _HttpResponse(status_code=200, text="{}")

    registry = PlaygroundRegistryClient(
        base_url="https://p.example",
        http_delete=fake_deregister,
    )
    vast = VastAiClient(api_key="k", http_delete=fake_vast_delete)

    run_destroy_sequence(
        reason="idle",
        worker_id="tts-a",
        vm_instance_id="42",
        registry_client=registry,
        vast_client=vast,
    )

    assert deregister_calls == ["https://p.example/playground/workers/tts-a"]
    assert destroy_calls == ["https://console.vast.ai/api/v0/instances/42/"]


def test_destroy_sequence_continues_past_registry_failure() -> None:
    destroy_calls: list[str] = []

    def bad_deregister(*, url: str, timeout: float) -> _HttpResponse:
        return _HttpResponse(status_code=500, text="oops")

    def fake_vast_delete(*, url: str, headers: dict[str, str], timeout: float) -> object:
        destroy_calls.append(url)
        return _HttpResponse(status_code=200, text="")

    registry = PlaygroundRegistryClient(
        base_url="https://p.example", http_delete=bad_deregister
    )
    vast = VastAiClient(api_key="k", http_delete=fake_vast_delete)

    run_destroy_sequence(
        reason="idle",
        worker_id="tts-a",
        vm_instance_id="42",
        registry_client=registry,
        vast_client=vast,
    )

    assert destroy_calls == ["https://console.vast.ai/api/v0/instances/42/"]


def test_destroy_sequence_skips_vast_when_no_instance_id() -> None:
    vast_calls: list[str] = []

    def fake_vast_delete(*, url: str, headers: dict[str, str], timeout: float) -> object:
        vast_calls.append(url)
        return _HttpResponse(status_code=200, text="")

    vast = VastAiClient(api_key="k", http_delete=fake_vast_delete)

    run_destroy_sequence(
        reason="manual",
        worker_id="local-dev",
        vm_instance_id=None,
        registry_client=None,
        vast_client=vast,
    )

    assert vast_calls == []


def test_destroy_sequence_swallows_vast_error() -> None:
    def boom(*, url: str, headers: dict[str, str], timeout: float) -> object:
        raise RuntimeError("connection reset")

    vast = VastAiClient(api_key="k", http_delete=boom)

    # Must not raise; process will exit regardless.
    run_destroy_sequence(
        reason="lifetime",
        worker_id="w",
        vm_instance_id="42",
        registry_client=None,
        vast_client=vast,
    )


def test_tick_loop_fires_on_destroy_when_idle_elapsed() -> None:
    state = GuardianState(boot_ts=1_000.0, last_bump_ts=1_000.0)
    config = GuardianConfig(idle_budget_s=30, max_lifetime_budget_s=3_600)
    telemetry = _make_telemetry()
    captured: list[DestroyReason] = []
    stop = threading.Event()

    class _Clock:
        def __init__(self) -> None:
            self.value = 1_000.0

        def __call__(self) -> float:
            return self.value

    clock = _Clock()

    def on_destroy(reason: DestroyReason) -> None:
        captured.append(reason)
        stop.set()

    clock.value = 1_100.0  # idle budget long since expired

    decision_tick_loop(
        state=state,
        config=config,
        telemetry=telemetry,
        on_destroy=on_destroy,
        stop_event=stop,
        tick_interval_s=0.01,
        clock=clock,
    )

    assert captured == ["idle"]


def test_tick_loop_does_not_fire_when_alive() -> None:
    state = GuardianState(boot_ts=1_000.0, last_bump_ts=1_000.0)
    config = GuardianConfig(idle_budget_s=3_600, max_lifetime_budget_s=86_400)
    telemetry = _make_telemetry()
    captured: list[DestroyReason] = []
    stop = threading.Event()

    clock_value = {"v": 1_000.0}

    def clock() -> float:
        # Bump the stop event after a couple iterations.
        if clock_value["v"] > 1_000.2:
            stop.set()
        clock_value["v"] += 0.05
        return clock_value["v"]

    decision_tick_loop(
        state=state,
        config=config,
        telemetry=telemetry,
        on_destroy=lambda r: captured.append(r),
        stop_event=stop,
        tick_interval_s=0.01,
        clock=clock,
    )

    assert captured == []


def test_tick_loop_sampling_updates_peak(monkeypatch: pytest.MonkeyPatch) -> None:
    state = GuardianState(boot_ts=1_000.0, last_bump_ts=1_000.0)
    config = GuardianConfig(idle_budget_s=3_600, max_lifetime_budget_s=86_400)
    samples: list[tuple[int, int]] = [(80, 10), (80, 20), (80, 30)]
    sample_iter = iter(samples)
    telemetry = ResourceTelemetry(
        vram_prober=lambda: next(sample_iter, (80, 30)),
        disk_prober=lambda _: (500, 30),
    )
    stop = threading.Event()
    iterations = {"n": 0}

    def clock() -> float:
        iterations["n"] += 1
        if iterations["n"] >= 3:
            stop.set()
        return 1_000.0 + iterations["n"] * 0.1

    decision_tick_loop(
        state=state,
        config=config,
        telemetry=telemetry,
        on_destroy=lambda _: None,
        stop_event=stop,
        tick_interval_s=0.01,
        clock=clock,
    )

    snap = telemetry.sample()
    assert snap.vram_peak_gb is not None
    assert snap.vram_peak_gb >= 20  # sampler was called at least twice
