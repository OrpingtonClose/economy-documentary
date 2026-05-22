"""FastAPI surface for the Qwen3-TTS worker.

Plain-text protocol — two endpoints only:

* ``GET /`` — plain text status. Does NOT bump the infra agent.
* ``POST /`` — receives plain text, returns raw WAV bytes.

Every POST / runs through a middleware that fires a best-effort bump
against ``http://localhost:29230/``.

The app is built by :func:`build_app` with injected dependencies so
unit tests do not need a GPU, a model, or a network.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from strands_agents.infra_agent.telemetry import ResourceTelemetry

from .bump_client import InfraAgentBumpClient
from .engine import SynthesisRequest, TTSEngine, TTSEngineError

logger = logging.getLogger(__name__)


class _BumpOnRequestMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that bumps the infra agent on each request.

    Paths can be excluded so that uptime probes do not accidentally
    keep VMs alive.
    """

    def __init__(
        self,
        app: Any,
        *,
        bump_client: InfraAgentBumpClient,
        excluded_paths: tuple[str, ...] = ("/",),
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
        engine: The TTS engine. Real in production, stub in tests.
        telemetry: Peak VRAM / disk telemetry (shared with the infra
            agent).
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

    @app.get("/")
    def _health() -> Response:
        """Liveness only — does not bump."""
        return Response(
            content=f"ok worker_id={worker_id}",
            media_type="text/plain",
        )

    @app.post("/")
    async def _render(request: Request) -> Response:
        body = await request.body()
        text = body.decode("utf-8").strip()
        if not text:
            return Response(
                content="error: empty text",
                media_type="text/plain",
                status_code=400,
            )

        try:
            result = engine.synthesize(
                SynthesisRequest(
                    text=text,
                    voice_id=pinned_voice_id,
                    language="en",
                    style=None,
                    seed=None,
                )
            )
        except TTSEngineError as exc:
            logger.error(
                "worker_id=<%s>, error=<%s> | engine refused synthesis",
                worker_id,
                exc,
            )
            return Response(
                content=f"error: {exc}",
                media_type="text/plain",
                status_code=400,
            )

        return Response(
            content=result.wav_bytes,
            media_type="audio/wav",
            headers={
                "X-Duration-S": str(result.duration_s),
                "X-Sample-Rate": str(result.sample_rate_hz),
            },
        )

    return app
