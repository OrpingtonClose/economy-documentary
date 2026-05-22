"""FastAPI surface for the LTX-Video worker.

Plain-text protocol — two endpoints only:

* ``GET /`` — plain text status. Does NOT bump the infra agent.
* ``POST /`` — receives a plain text prompt, returns raw MP4 bytes.

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
from .engine import (
    MAX_DURATION_S,
    MIN_DURATION_S,
    RenderRequest,
    VideoEngine,
    VideoEngineError,
)

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
            agent).
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
        prompt = body.decode("utf-8").strip()
        if not prompt:
            return Response(
                content="error: empty prompt",
                media_type="text/plain",
                status_code=400,
            )

        try:
            result = engine.render(
                RenderRequest(
                    prompt=prompt,
                    duration_s=5.0,
                    width=512,
                    height=320,
                    fps=24,
                    style=None,
                    seed=42,
                    negative_prompt=None,
                )
            )
        except VideoEngineError as exc:
            logger.error(
                "worker_id=<%s>, error=<%s> | engine refused render",
                worker_id,
                exc,
            )
            return Response(
                content=f"error: {exc}",
                media_type="text/plain",
                status_code=400,
            )

        return Response(
            content=result.mp4_bytes,
            media_type="video/mp4",
            headers={
                "X-Duration-S": str(result.duration_s),
                "X-Width": str(result.width),
                "X-Height": str(result.height),
                "X-FPS": str(result.fps),
            },
        )

    return app
