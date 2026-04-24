"""Unit tests for the LTX-Video worker runner."""

from __future__ import annotations

import argparse
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from strands_agents.infra_agent.telemetry import ResourceTelemetry
from strands_agents.ltx_video_worker import runner
from strands_agents.ltx_video_worker.registry_client import (
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


class _RaisingTelemetry:
    def sample(self) -> Any:
        raise RuntimeError("disk read blew up")


def _make_telemetry(vram: tuple[int, int] = (141, 12)) -> ResourceTelemetry:
    return ResourceTelemetry(
        vram_prober=lambda: vram,
        disk_prober=lambda _p: (500, 40),
        disk_path="/",
    )


def _ns(**overrides: Any) -> argparse.Namespace:
    base = dict(
        worker_id=None,
        endpoint_url=None,
        playground_base_url=None,
        vram_gb=None,
        bump_url=None,
        disk_path=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_resolve_config_requires_worker_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WORKER_ID", raising=False)
    with pytest.raises(SystemExit):
        runner._resolve_config(_ns())


def test_resolve_config_env_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_ID", "ltx-env")
    monkeypatch.setenv("WORKER_VRAM_GB", "141")
    monkeypatch.setenv("PLAYGROUND_BACKEND_URL", "https://p.example")
    monkeypatch.setenv(
        "INFRA_AGENT_BUMP_URL", "http://localhost:29230/infra/bump"
    )
    monkeypatch.setenv("PUBLIC_IPADDR", "203.0.113.5")

    cfg = runner._resolve_config(_ns())
    assert cfg.worker_id == "ltx-env"
    assert cfg.vram_gb == 141
    assert cfg.playground_base_url == "https://p.example"
    assert cfg.bump_url == "http://localhost:29230/infra/bump"
    assert cfg.endpoint_url == f"http://203.0.113.5:{runner.WORKER_PORT}"


def test_resolve_config_cli_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_ID", "env-id")
    monkeypatch.setenv("WORKER_VRAM_GB", "48")
    cfg = runner._resolve_config(
        _ns(
            worker_id="cli-id",
            endpoint_url="http://x:1234",
            vram_gb=80,
            disk_path="/data",
        )
    )
    assert cfg.worker_id == "cli-id"
    assert cfg.endpoint_url == "http://x:1234"
    assert cfg.vram_gb == 80
    assert cfg.disk_path == "/data"


def test_resolve_config_defaults_public_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_ID", "w1")
    monkeypatch.delenv("PUBLIC_IPADDR", raising=False)
    monkeypatch.delenv("WORKER_ENDPOINT_URL", raising=False)
    cfg = runner._resolve_config(_ns())
    assert cfg.endpoint_url == f"http://127.0.0.1:{runner.WORKER_PORT}"


def test_resolve_config_defaults_disk_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_ID", "w1")
    monkeypatch.delenv("WORKER_DISK_PATH", raising=False)
    cfg = runner._resolve_config(_ns())
    assert cfg.disk_path == "/"


def test_resolve_config_vram_zero_is_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_ID", "w1")
    # Explicit CLI 0 must NOT be silently overridden by the env default.
    cfg = runner._resolve_config(_ns(vram_gb=0))
    assert cfg.vram_gb == 0


def test_resolve_config_defaults_vram_to_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_ID", "w1")
    monkeypatch.delenv("WORKER_VRAM_GB", raising=False)
    cfg = runner._resolve_config(_ns())
    assert cfg.vram_gb == runner.DEFAULT_LOCAL_VRAM_GB


def test_env_int_fallback_on_non_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_VRAM_GB", "not-a-number")
    assert runner._env_int("WORKER_VRAM_GB", default=77) == 77


def test_env_int_fallback_on_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_VRAM_GB", raising=False)
    assert runner._env_int("WORKER_VRAM_GB", default=99) == 99


def test_env_int_fallback_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_VRAM_GB", "   ")
    assert runner._env_int("WORKER_VRAM_GB", default=5) == 5


def _drive_loop_once(
    *,
    registry_client: Any,
    telemetry: Any,
    interval_s: float = 30.0,
) -> _StepClock:
    """Drive heartbeat_loop for one tick, then stop."""
    clock = _StepClock()
    stop = threading.Event()
    done = threading.Event()

    def _run() -> None:
        runner.heartbeat_loop(
            worker_id="w1",
            registry_client=registry_client,
            telemetry=telemetry,
            stop_event=stop,
            interval_s=interval_s,
            clock=clock,
        )
        done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    # Give the loop a moment to fire the first tick at t=0.
    done_first = done.wait(timeout=0.2)
    assert not done_first  # still running
    stop.set()
    done.wait(timeout=2.0)
    assert done.is_set()
    return clock


def test_heartbeat_loop_fires_first_tick_with_free_vram() -> None:
    client = _FakeRegistryClient()
    telemetry = _make_telemetry(vram=(141, 20))
    _drive_loop_once(registry_client=client, telemetry=telemetry)
    assert len(client.heartbeat_calls) >= 1
    call = client.heartbeat_calls[0]
    assert call["worker_id"] == "w1"
    assert call["free_vram_gb"] == 141 - 20


def test_heartbeat_loop_swallows_registry_error() -> None:
    client = _FakeRegistryClient(heartbeat_error=RegistryHeartbeatError("5xx"))
    telemetry = _make_telemetry()
    _drive_loop_once(registry_client=client, telemetry=telemetry)
    # Loop must survive the error and try at least once.
    assert len(client.heartbeat_calls) >= 1


def test_heartbeat_loop_swallows_telemetry_error() -> None:
    client = _FakeRegistryClient()
    telemetry = _RaisingTelemetry()
    _drive_loop_once(registry_client=client, telemetry=telemetry)
    # Telemetry blew up → free_vram_gb must default to None and loop continues.
    assert len(client.heartbeat_calls) >= 1
    assert client.heartbeat_calls[0]["free_vram_gb"] is None


def test_heartbeat_loop_stops_promptly_on_stop_event() -> None:
    client = _FakeRegistryClient()
    telemetry = _make_telemetry()
    stop = threading.Event()
    clock = _StepClock()

    def _run() -> None:
        runner.heartbeat_loop(
            worker_id="w1",
            registry_client=client,
            telemetry=telemetry,
            stop_event=stop,
            interval_s=30.0,
            clock=clock,
        )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    stop.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_heartbeat_loop_passes_none_when_vram_probe_returns_none() -> None:
    client = _FakeRegistryClient()
    telemetry = ResourceTelemetry(
        vram_prober=lambda: (None, None),
        disk_prober=lambda _p: (500, 10),
        disk_path="/",
    )
    _drive_loop_once(registry_client=client, telemetry=telemetry)
    assert client.heartbeat_calls[0]["free_vram_gb"] is None


def test_parse_args_all_optional() -> None:
    ns = runner._parse_args([])
    assert ns.worker_id is None
    assert ns.endpoint_url is None
    assert ns.vram_gb is None


def test_parse_args_accepts_full_flags() -> None:
    ns = runner._parse_args(
        [
            "--worker-id",
            "w1",
            "--endpoint-url",
            "http://x:1",
            "--playground-base-url",
            "https://p",
            "--vram-gb",
            "141",
            "--bump-url",
            "http://localhost:29230/infra/bump",
            "--disk-path",
            "/data",
        ]
    )
    assert ns.worker_id == "w1"
    assert ns.vram_gb == 141
    assert ns.disk_path == "/data"


def test_real_video_engine_factory_falls_back_to_stub_when_ltx_missing() -> None:
    # The real engine module does not exist in the repo — factory must
    # catch ImportError and return a StubVideoEngine so CI doesn't fail.
    engine = runner._real_video_engine_factory()
    assert engine.engine_id == "stub"


def test_worker_port_is_29232() -> None:
    assert runner.WORKER_PORT == 29232


def test_heartbeat_interval_is_30_seconds() -> None:
    assert runner.HEARTBEAT_INTERVAL_S == 30.0
