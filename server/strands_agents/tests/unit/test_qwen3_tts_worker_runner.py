"""Unit tests for the Qwen3-TTS worker runner."""

from __future__ import annotations

import argparse
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from strands_agents.infra_agent.telemetry import ResourceTelemetry
from strands_agents.qwen3_tts_worker import runner
from strands_agents.qwen3_tts_worker.registry_client import (
    RegistryHeartbeatError,
)


@dataclass
class _FakeRegistryClient:
    heartbeat_calls: list[dict[str, Any]] = field(default_factory=list)
    heartbeat_error: Exception | None = None

    def heartbeat(
        self, *, worker_id: str, free_vram_gb: int | None = None
    ) -> dict:
        self.heartbeat_calls.append(
            {"worker_id": worker_id, "free_vram_gb": free_vram_gb}
        )
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        return {}


class _StepClock:
    """Deterministic monotonic-like clock."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _make_telemetry(vram: tuple[int, int] = (24, 6)) -> ResourceTelemetry:
    return ResourceTelemetry(
        vram_prober=lambda: vram,
        disk_prober=lambda _p: (500, 40),
        disk_path="/",
    )


def test_resolve_config_requires_worker_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_ID", raising=False)
    with pytest.raises(SystemExit):
        runner._resolve_config(
            argparse.Namespace(
                worker_id=None,
                voice_id="alex",
                endpoint_url=None,
                playground_base_url=None,
                vram_gb=None,
                bump_url=None,
                disk_path=None,
            )
        )


def test_resolve_config_requires_voice_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_VOICE_ID", raising=False)
    with pytest.raises(SystemExit):
        runner._resolve_config(
            argparse.Namespace(
                worker_id="w1",
                voice_id=None,
                endpoint_url=None,
                playground_base_url=None,
                vram_gb=None,
                bump_url=None,
                disk_path=None,
            )
        )


def test_resolve_config_env_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_ID", "w-env")
    monkeypatch.setenv("WORKER_VOICE_ID", "alex")
    monkeypatch.setenv("WORKER_VRAM_GB", "48")
    monkeypatch.setenv("PLAYGROUND_BACKEND_URL", "https://p.example")
    monkeypatch.setenv("INFRA_AGENT_BUMP_URL", "http://localhost:29230/infra/bump")
    monkeypatch.setenv("PUBLIC_IPADDR", "203.0.113.5")

    cfg = runner._resolve_config(
        argparse.Namespace(
            worker_id=None,
            voice_id=None,
            endpoint_url=None,
            playground_base_url=None,
            vram_gb=None,
            bump_url=None,
            disk_path=None,
        )
    )
    assert cfg.worker_id == "w-env"
    assert cfg.voice_id == "alex"
    assert cfg.vram_gb == 48
    assert cfg.playground_base_url == "https://p.example"
    assert cfg.bump_url == "http://localhost:29230/infra/bump"
    assert cfg.endpoint_url == f"http://203.0.113.5:{runner.WORKER_PORT}"


def test_resolve_config_cli_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_ID", "env-id")
    monkeypatch.setenv("WORKER_VOICE_ID", "env-voice")
    cfg = runner._resolve_config(
        argparse.Namespace(
            worker_id="cli-id",
            voice_id="cli-voice",
            endpoint_url="http://x:1234",
            playground_base_url=None,
            vram_gb=12,
            bump_url=None,
            disk_path="/data",
        )
    )
    assert cfg.worker_id == "cli-id"
    assert cfg.voice_id == "cli-voice"
    assert cfg.endpoint_url == "http://x:1234"
    assert cfg.vram_gb == 12
    assert cfg.disk_path == "/data"


def test_env_int_fallback_on_non_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_VRAM_GB", "not-a-number")
    assert runner._env_int("WORKER_VRAM_GB", default=77) == 77


def test_heartbeat_loop_fires_on_interval_and_stops() -> None:
    client = _FakeRegistryClient()
    telemetry = _make_telemetry()
    clock = _StepClock()
    stop = threading.Event()

    # Drive one tick at t=0, advance, second at t=30, then stop.
    done = threading.Event()

    def _drive() -> None:
        runner.heartbeat_loop(
            worker_id="w1",
            registry_client=client,  # type: ignore[arg-type]
            telemetry=telemetry,
            stop_event=stop,
            interval_s=30.0,
            clock=clock,
        )
        done.set()

    thread = threading.Thread(target=_drive, daemon=True)
    thread.start()
    # Let first tick (at t=0) land.
    _wait_until(lambda: len(client.heartbeat_calls) >= 1, timeout=2.0)
    clock.advance(30.0)
    _wait_until(lambda: len(client.heartbeat_calls) >= 2, timeout=2.0)
    stop.set()
    done.wait(timeout=2.0)
    assert done.is_set()

    assert client.heartbeat_calls[0]["worker_id"] == "w1"
    # free_vram_gb computed from snapshot (24 total, 6 used → 18 free).
    assert client.heartbeat_calls[0]["free_vram_gb"] == 18


def test_heartbeat_loop_swallows_registry_error() -> None:
    client = _FakeRegistryClient(
        heartbeat_error=RegistryHeartbeatError("transient")
    )
    telemetry = _make_telemetry()
    clock = _StepClock()
    stop = threading.Event()
    done = threading.Event()

    def _drive() -> None:
        runner.heartbeat_loop(
            worker_id="w1",
            registry_client=client,  # type: ignore[arg-type]
            telemetry=telemetry,
            stop_event=stop,
            interval_s=30.0,
            clock=clock,
        )
        done.set()

    thread = threading.Thread(target=_drive, daemon=True)
    thread.start()
    _wait_until(lambda: len(client.heartbeat_calls) >= 1, timeout=2.0)
    stop.set()
    done.wait(timeout=2.0)
    assert done.is_set()


def test_heartbeat_loop_omits_free_vram_when_probe_missing() -> None:
    client = _FakeRegistryClient()
    telemetry = ResourceTelemetry(
        vram_prober=lambda: None,
        disk_prober=lambda _p: (500, 40),
        disk_path="/",
    )
    clock = _StepClock()
    stop = threading.Event()
    done = threading.Event()

    def _drive() -> None:
        runner.heartbeat_loop(
            worker_id="w1",
            registry_client=client,  # type: ignore[arg-type]
            telemetry=telemetry,
            stop_event=stop,
            interval_s=30.0,
            clock=clock,
        )
        done.set()

    thread = threading.Thread(target=_drive, daemon=True)
    thread.start()
    _wait_until(lambda: len(client.heartbeat_calls) >= 1, timeout=2.0)
    stop.set()
    done.wait(timeout=2.0)
    assert client.heartbeat_calls[0]["free_vram_gb"] is None


def test_real_engine_factory_falls_back_to_stub_when_backend_missing() -> None:
    engine = runner._real_tts_engine_factory()
    # No qwen3 backend module exists → must fall back to the stub.
    assert engine.engine_id == "stub"


def _wait_until(predicate: Any, *, timeout: float) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("predicate did not become true within timeout")
