"""Unit tests for the Qwen3-TTS worker FastAPI surface."""

from __future__ import annotations

import base64
import io
import wave
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from strands_agents.infra_agent.telemetry import ResourceTelemetry
from strands_agents.qwen3_tts_worker.app import build_app
from strands_agents.qwen3_tts_worker.bump_client import InfraAgentBumpClient
from strands_agents.qwen3_tts_worker.engine import (
    StubTTSEngine,
    SynthesisRequest,
    SynthesisResult,
    TTSEngineError,
)


@dataclass
class _BumpCounter:
    """Fake POST that records each call for bump-on-request assertions."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, *, url: str, timeout: float) -> object:
        self.calls.append({"url": url, "timeout": timeout})

        @dataclass(frozen=True)
        class _R:
            status_code: int

        return _R(status_code=200)


class _RaisingEngine:
    """Engine stub that always raises, for the error-path test."""

    @property
    def engine_id(self) -> str:
        return "raising"

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        raise TTSEngineError("weights unavailable")


@pytest.fixture
def setup() -> dict[str, Any]:
    bump_post = _BumpCounter()
    bump_client = InfraAgentBumpClient(
        url="http://127.0.0.1:29230/infra/bump", http_post=bump_post
    )
    telemetry = ResourceTelemetry(
        vram_prober=lambda: (24, 4),
        disk_prober=lambda _path: (500, 20),
        disk_path="/",
    )
    engine = StubTTSEngine(chars_per_second=20.0)
    app = build_app(
        worker_id="tts-alex-01",
        pinned_voice_id="alex",
        engine=engine,
        telemetry=telemetry,
        bump_client=bump_client,
    )
    return {
        "client": TestClient(app),
        "bump_post": bump_post,
        "telemetry": telemetry,
        "engine": engine,
    }


def test_health_does_not_bump(setup: dict[str, Any]) -> None:
    response = setup["client"].get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "worker_id": "tts-alex-01"}
    assert setup["bump_post"].calls == []


def test_render_happy_path_returns_wav_and_bumps(setup: dict[str, Any]) -> None:
    response = setup["client"].post(
        "/tts/render",
        json={"text": "Hello narrator.", "voice_id": "alex"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["worker_id"] == "tts-alex-01"
    assert body["voice_id"] == "alex"
    assert body["engine"] == "stub"
    assert body["duration_s"] > 0
    assert body["sample_rate_hz"] == 24_000

    wav_bytes = base64.b64decode(body["wav_base64"])
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 24_000
        assert wav.getnframes() > 0

    assert len(setup["bump_post"].calls) == 1


def test_render_voice_mismatch_409(setup: dict[str, Any]) -> None:
    response = setup["client"].post(
        "/tts/render",
        json={"text": "hi", "voice_id": "morgan"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "voice_mismatch"
    assert detail["pinned_voice_id"] == "alex"
    assert detail["requested_voice_id"] == "morgan"
    # Voice mismatch still goes through middleware → still bumps.
    assert len(setup["bump_post"].calls) == 1


def test_render_rejects_empty_text(setup: dict[str, Any]) -> None:
    response = setup["client"].post(
        "/tts/render", json={"text": "", "voice_id": "alex"}
    )
    assert response.status_code == 422


def test_render_engine_error_returns_400() -> None:
    bump_post = _BumpCounter()
    app = build_app(
        worker_id="tts-broken",
        pinned_voice_id="alex",
        engine=_RaisingEngine(),
        telemetry=ResourceTelemetry(
            vram_prober=lambda: (24, 4),
            disk_prober=lambda _p: (500, 20),
            disk_path="/",
        ),
        bump_client=InfraAgentBumpClient(http_post=bump_post),
    )
    response = TestClient(app).post(
        "/tts/render", json={"text": "hi there", "voice_id": "alex"}
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["reason"] == "engine_error"
    assert "weights unavailable" in detail["message"]


def test_vram_endpoint_returns_peaks(setup: dict[str, Any]) -> None:
    response = setup["client"].get("/health/vram")
    assert response.status_code == 200
    body = response.json()
    assert body["worker_id"] == "tts-alex-01"
    assert body["vram_total_gb"] == 24
    assert body["vram_used_gb"] == 4
    assert body["vram_peak_gb"] == 4
    assert body["disk_total_gb"] == 500
    assert body["disk_peak_gb"] == 20


def test_vram_endpoint_bumps_infra_agent(setup: dict[str, Any]) -> None:
    setup["client"].get("/health/vram")
    assert len(setup["bump_post"].calls) == 1


def test_bump_fires_on_every_request(setup: dict[str, Any]) -> None:
    client = setup["client"]
    client.post("/tts/render", json={"text": "first", "voice_id": "alex"})
    client.post("/tts/render", json={"text": "second", "voice_id": "alex"})
    client.get("/health/vram")
    client.get("/health")  # excluded from middleware
    assert len(setup["bump_post"].calls) == 3


def test_bump_failure_does_not_break_render(setup: dict[str, Any]) -> None:
    # Flip the bump to raise and confirm the render still succeeds.
    bump_post = setup["bump_post"]

    def _raise(**_: Any) -> None:
        raise RuntimeError("network partitioned")

    telemetry = ResourceTelemetry(
        vram_prober=lambda: (24, 4),
        disk_prober=lambda _p: (500, 20),
        disk_path="/",
    )
    app = build_app(
        worker_id="tts-iso",
        pinned_voice_id="alex",
        engine=StubTTSEngine(),
        telemetry=telemetry,
        bump_client=InfraAgentBumpClient(http_post=_raise),
    )
    response = TestClient(app).post(
        "/tts/render", json={"text": "still works", "voice_id": "alex"}
    )
    assert response.status_code == 200
    # Separate test, but assert the main fixture's bump counter is untouched.
    assert bump_post.calls == []
