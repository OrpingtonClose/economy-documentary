"""FastAPI surface for the LTX-Video worker.

Three endpoints:

* ``POST /video/render`` — render one scene clip. Returns metadata +
  base64-encoded MP4.
* ``GET /health/vram`` — current and peak VRAM, used by the playground
  pre-flight check and the worker registry.
* ``GET /health`` — lightweight liveness. Does NOT bump the infra
  agent — otherwise k8s-style pollers would keep the VM alive forever.

Every request (except ``/health``) runs through a middleware that fires
a best-effort bump against ``http://localhost:29230/infra/bump``.
Active traffic = alive VM, by construction.

The app is built by :func:`build_app` with injected dependencies so
unit tests do not need a GPU, a model, or a network.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from strands_agents.infra_agent.telemetry import ResourceTelemetry

from .bump_client import InfraAgentBumpClient
from .engine import (
    MAX_DURATION_S,
    MIN_DURATION_S,
    RenderRequest,
    VideoEngine,
    VideoEngineError,
)

logger = logging.getLogger(__name__)


class _RenderRequest(BaseModel):
    """``POST /video/render`` body.

    Bounds mirror :mod:`engine` constants; the engine re-validates and
    clamps duration, but the API rejects obvious garbage early.
    """

    prompt: str = Field(..., min_length=1, max_length=4_096)
    duration_s: float = Field(
        ..., gt=0.0, le=MAX_DURATION_S * 2
    )  # engine clamps; allow a soft upper so out-of-range is 400 at schema
    width: int = Field(default=1280, ge=256, le=3840)
    height: int = Field(default=720, ge=256, le=2160)
    fps: int = Field(default=24, ge=1, le=60)
    style: str | None = Field(default=None, max_length=128)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    negative_prompt: str | None = Field(default=None, max_length=4_096)


class _BumpOnRequestMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that bumps the infra agent on each request.

    Paths can be excluded (e.g. ``/health``) so that uptime probes do
    not accidentally keep VMs alive.
    """

    def __init__(
        self,
        app: Any,
        *,
        bump_client: InfraAgentBumpClient,
        excluded_paths: tuple[str, ...] = ("/health",),
    ) -> None:
        super().__init__(app)
        self._bump_client = bump_client
        self._excluded_paths = excluded_paths

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        if request.url.path not in self._excluded_paths:
            self._bump_client.bump()
        response: StarletteResponse = await call_next(request)
        return response


def build_app(
    *,
    worker_id: str,
    engine: VideoEngine,
    telemetry: ResourceTelemetry,
    bump_client: InfraAgentBumpClient | None = None,
) -> FastAPI:
    """Construct the worker FastAPI app.

    Args:
        worker_id: Registry id for this worker. Stamped on every
            response for tracing.
        engine: The video engine. Real in production, stub in tests.
        telemetry: Peak VRAM / disk telemetry (shared with the infra
            agent). Reused here so ``/health/vram`` returns the same
            numbers the guardian publishes.
        bump_client: Injectable bump client. Defaults to a fresh
            :class:`InfraAgentBumpClient` pointing at localhost.

    Returns:
        A ready-to-serve FastAPI app.
    """
    effective_bump = bump_client or InfraAgentBumpClient()
    app = FastAPI(
        title=f"ltx-video-worker[{worker_id}]",
        description="Per-VM LTX-Video renderer",
    )
    app.add_middleware(
        _BumpOnRequestMiddleware,
        bump_client=effective_bump,
    )

    router_video = APIRouter(prefix="/video", tags=["video"])
    router_health = APIRouter(prefix="/health", tags=["health"])

    @app.get("/health")
    def _health() -> dict[str, Any]:
        """Liveness only — does not bump."""
        return {"ok": True, "worker_id": worker_id}

    @router_video.post("/render")
    def _render(body: _RenderRequest) -> JSONResponse:
        if body.duration_s < MIN_DURATION_S:
            raise HTTPException(
                status_code=400,
                detail={
                    "reason": "duration_too_short",
                    "min_duration_s": MIN_DURATION_S,
                    "requested_duration_s": body.duration_s,
                },
            )

        try:
            result = engine.render(
                RenderRequest(
                    prompt=body.prompt,
                    duration_s=body.duration_s,
                    width=body.width,
                    height=body.height,
                    fps=body.fps,
                    style=body.style,
                    seed=body.seed,
                    negative_prompt=body.negative_prompt,
                )
            )
        except VideoEngineError as exc:
            logger.error(
                "worker_id=<%s>, error=<%s> | engine refused render",
                worker_id,
                exc,
            )
            raise HTTPException(
                status_code=400,
                detail={"reason": "engine_error", "message": str(exc)},
            ) from exc

        return JSONResponse(
            content={
                "worker_id": worker_id,
                "engine": result.engine,
                "duration_s": result.duration_s,
                "width": result.width,
                "height": result.height,
                "fps": result.fps,
                "mp4_base64": base64.b64encode(result.mp4_bytes).decode("ascii"),
                "mp4_bytes": len(result.mp4_bytes),
            }
        )

    @router_health.get("/vram")
    def _vram() -> JSONResponse:
        snapshot = telemetry.sample()
        return JSONResponse(
            content={
                "worker_id": worker_id,
                "vram_total_gb": snapshot.vram_total_gb,
                "vram_used_gb": snapshot.vram_used_gb,
                "vram_peak_gb": snapshot.vram_peak_gb,
                "disk_total_gb": snapshot.disk_total_gb,
                "disk_used_gb": snapshot.disk_used_gb,
                "disk_peak_gb": snapshot.disk_peak_gb,
            }
        )

    app.include_router(router_video)
    app.include_router(router_health)
    return app
