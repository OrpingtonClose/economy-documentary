"""FastAPI surface for the Qwen3-TTS worker.

Three endpoints:

* ``POST /tts/render`` — synthesize one utterance against the VM's
  pinned voice. Returns metadata + base64-encoded WAV.
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
from .engine import SynthesisRequest, TTSEngine, TTSEngineError

logger = logging.getLogger(__name__)


class _RenderRequest(BaseModel):
    """``POST /tts/render`` body.

    ``voice_id`` must equal the VM's pinned voice or the worker 409s.
    """

    text: str = Field(..., min_length=1, max_length=4_096)
    voice_id: str = Field(..., min_length=1, max_length=128)
    language: str = Field(default="en", min_length=2, max_length=16)
    style: str | None = Field(default=None, max_length=64)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)


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
    pinned_voice_id: str,
    engine: TTSEngine,
    telemetry: ResourceTelemetry,
    bump_client: InfraAgentBumpClient | None = None,
) -> FastAPI:
    """Construct the worker FastAPI app.

    Args:
        worker_id: Registry id for this worker. Stamped on every
            response for tracing.
        pinned_voice_id: The one voice this VM is allowed to render.
            Requests carrying any other ``voice_id`` 409.
        engine: The TTS engine. Real in production, stub in tests.
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
        title=f"qwen3-tts-worker[{worker_id}]",
        description="Per-VM TTS worker pinned to one voice",
    )
    app.add_middleware(
        _BumpOnRequestMiddleware,
        bump_client=effective_bump,
    )

    router_tts = APIRouter(prefix="/tts", tags=["tts"])
    router_health = APIRouter(prefix="/health", tags=["health"])

    @app.get("/health")
    def _health() -> dict[str, Any]:
        """Liveness only — does not bump."""
        return {"ok": True, "worker_id": worker_id}

    @router_tts.post("/render")
    def _render(body: _RenderRequest) -> JSONResponse:
        if body.voice_id != pinned_voice_id:
            logger.warning(
                "worker_id=<%s>, pinned=<%s>, got=<%s> | voice mismatch, rejecting",
                worker_id,
                pinned_voice_id,
                body.voice_id,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "voice_mismatch",
                    "pinned_voice_id": pinned_voice_id,
                    "requested_voice_id": body.voice_id,
                },
            )

        try:
            result = engine.synthesize(
                SynthesisRequest(
                    text=body.text,
                    voice_id=body.voice_id,
                    language=body.language,
                    style=body.style,
                    seed=body.seed,
                )
            )
        except TTSEngineError as exc:
            logger.error(
                "worker_id=<%s>, error=<%s> | engine refused synthesis",
                worker_id,
                exc,
            )
            raise HTTPException(
                status_code=400, detail={"reason": "engine_error", "message": str(exc)}
            ) from exc

        return JSONResponse(
            content={
                "worker_id": worker_id,
                "voice_id": result.voice_id,
                "engine": result.engine,
                "duration_s": result.duration_s,
                "sample_rate_hz": result.sample_rate_hz,
                "wav_base64": base64.b64encode(result.wav_bytes).decode("ascii"),
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

    app.include_router(router_tts)
    app.include_router(router_health)
    return app
