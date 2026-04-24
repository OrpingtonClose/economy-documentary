"""Unit tests for the LTX-Video worker FastAPI surface."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from strands_agents.infra_agent.telemetry import ResourceTelemetry
from strands_agents.ltx_video_worker.app import build_app
from strands_agents.ltx_video_worker.bump_client import InfraAgentBumpClient
from strands_agents.ltx_video_worker.engine import (
    MAX_DURATION_S,
    RenderRequest,
    RenderResult,
    StubVideoEngine,
    VideoEngineError,
)


@dataclass
class _BumpCounter:
    """Fake POST recording bump calls for middleware assertions."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, *, url: str, timeout: float) -> object:
        self.calls.append({"url": url, "timeout": timeout})

        @dataclass(frozen=True)
        class _R:
            status_code: int

        return _R(status_code=200)


class _RaisingEngine:
    """Engine that always raises, for error-path coverage."""

    @property
    def engine_id(self) -> str:
        return "raising"

    def render(self, request: RenderRequest) -> RenderResult:
        raise VideoEngineError("weights unavailable")


def _setup() -> dict[str, Any]:
    bump_post = _BumpCounter()
    bump_client = InfraAgentBumpClient(
        url="http://127.0.0.1:29230/infra/bump", http_post=bump_post
    )
    telemetry = ResourceTelemetry(
        vram_prober=lambda: (141, 12),
        disk_prober=lambda _p: (500, 30),
        disk_path="/",
    )
    engine = StubVideoEngine(bytes_per_second=10_000)
    app = build_app(
        worker_id="ltx-01",
        engine=engine,
        telemetry=telemetry,
        bump_client=bump_client,
    )
    return {
        "client": TestClient(app),
        "bump_post": bump_post,
        "telemetry": telemetry,
    }


def test_health_ok_does_not_bump() -> None:
    s = _setup()
    response = s["client"].get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "worker_id": "ltx-01"}
    assert s["bump_post"].calls == []


def test_render_happy_path_returns_mp4_and_bumps() -> None:
    s = _setup()
    response = s["client"].post(
        "/video/render",
        json={"prompt": "A river at dawn.", "duration_s": 1.0},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["worker_id"] == "ltx-01"
    assert body["engine"] == "stub"
    assert body["duration_s"] == 1.0
    assert body["width"] == 1280
    assert body["height"] == 720
    assert body["fps"] == 24

    mp4 = base64.b64decode(body["mp4_base64"])
    assert mp4[4:8] == b"ftyp"
    assert body["mp4_bytes"] == len(mp4)
    assert len(s["bump_post"].calls) == 1


def test_render_respects_custom_dimensions_and_fps() -> None:
    s = _setup()
    response = s["client"].post(
        "/video/render",
        json={
            "prompt": "p",
            "duration_s": 1.0,
            "width": 1920,
            "height": 1080,
            "fps": 30,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["width"] == 1920
    assert body["height"] == 1080
    assert body["fps"] == 30


def test_render_rejects_empty_prompt() -> None:
    s = _setup()
    response = s["client"].post(
        "/video/render", json={"prompt": "", "duration_s": 1.0}
    )
    assert response.status_code == 422


def test_render_rejects_negative_duration() -> None:
    s = _setup()
    response = s["client"].post(
        "/video/render", json={"prompt": "p", "duration_s": -1.0}
    )
    assert response.status_code == 422


def test_render_rejects_duration_beyond_soft_upper() -> None:
    s = _setup()
    response = s["client"].post(
        "/video/render",
        json={"prompt": "p", "duration_s": MAX_DURATION_S * 2 + 1},
    )
    assert response.status_code == 422


def test_render_rejects_oversize_width() -> None:
    s = _setup()
    response = s["client"].post(
        "/video/render",
        json={"prompt": "p", "duration_s": 1.0, "width": 9999},
    )
    assert response.status_code == 422


def test_render_rejects_too_small_dimensions() -> None:
    s = _setup()
    response = s["client"].post(
        "/video/render",
        json={"prompt": "p", "duration_s": 1.0, "width": 10, "height": 10},
    )
    assert response.status_code == 422


def test_render_rejects_fps_out_of_range() -> None:
    s = _setup()
    response = s["client"].post(
        "/video/render", json={"prompt": "p", "duration_s": 1.0, "fps": 0}
    )
    assert response.status_code == 422


def test_render_engine_error_returns_400() -> None:
    bump_post = _BumpCounter()
    app = build_app(
        worker_id="ltx-broken",
        engine=_RaisingEngine(),
        telemetry=ResourceTelemetry(
            vram_prober=lambda: (141, 0),
            disk_prober=lambda _p: (500, 20),
            disk_path="/",
        ),
        bump_client=InfraAgentBumpClient(http_post=bump_post),
    )
    response = TestClient(app).post(
        "/video/render", json={"prompt": "hi", "duration_s": 1.0}
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["reason"] == "engine_error"
    assert "weights unavailable" in detail["message"]
    # Engine errors still go through the middleware → still bump.
    assert len(bump_post.calls) == 1


def test_vram_endpoint_returns_peaks() -> None:
    s = _setup()
    response = s["client"].get("/health/vram")
    assert response.status_code == 200
    body = response.json()
    assert body["worker_id"] == "ltx-01"
    assert body["vram_total_gb"] == 141
    assert body["vram_used_gb"] == 12
    assert body["vram_peak_gb"] == 12
    assert body["disk_total_gb"] == 500
    assert body["disk_peak_gb"] == 30


def test_vram_endpoint_bumps_infra_agent() -> None:
    s = _setup()
    s["client"].get("/health/vram")
    # /health/vram is on /health/* router but not in excluded_paths,
    # so it bumps. (Exclusion is exact match on "/health".)
    assert len(s["bump_post"].calls) == 1


def test_render_accepts_optional_style_seed_negative() -> None:
    s = _setup()
    response = s["client"].post(
        "/video/render",
        json={
            "prompt": "forest",
            "duration_s": 2.0,
            "style": "documentary",
            "seed": 42,
            "negative_prompt": "blurry",
        },
    )
    assert response.status_code == 200


def test_render_rejects_negative_seed() -> None:
    s = _setup()
    response = s["client"].post(
        "/video/render",
        json={"prompt": "p", "duration_s": 1.0, "seed": -1},
    )
    assert response.status_code == 422


def test_build_app_defaults_bump_client_when_none() -> None:
    telemetry = ResourceTelemetry(
        vram_prober=lambda: (141, 0),
        disk_prober=lambda _p: (500, 0),
        disk_path="/",
    )
    # bump_client=None should not crash; the default client will try a
    # real network call which we never trigger because /health is
    # excluded from the middleware.
    app = build_app(
        worker_id="ltx-02",
        engine=StubVideoEngine(),
        telemetry=telemetry,
        bump_client=None,
    )
    response = TestClient(app).get("/health")
    assert response.status_code == 200
