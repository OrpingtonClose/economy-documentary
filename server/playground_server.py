"""Minimal FastAPI entrypoint for the Component Playground staging VM.

``server.server:app`` mounts the full pipeline (dashboard, fleet,
agents) — none of which the playground needs. This module exposes
only the ``/playground`` router, the ``/agui`` AG-UI transport,
and a ``/health`` check so the sealed inspection workbench can run
on a CPU-only VM without dragging in GPU/TTS/B2/dashboard dependencies.

Used by ``scripts/playground_staging_bootstrap.sh`` — the supervisor
config points uvicorn at ``playground_server:app``.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

# Wire OpenTelemetry + the Langfuse exporter (slice 2 of the Wave 2/3
# pipeline). Guarded import so a deps drift (missing opentelemetry SDK,
# wrong Langfuse package pin) logs a warning instead of crashing the
# ``uvicorn`` worker — the frontend "View Trace" button hides itself
# when ``/playground/config/langfuse`` returns ``enabled: false``, so
# graceful degradation is wired end-to-end.
try:
    from strands_agents.playground.telemetry import (  # noqa: E402
        setup_playground_otel,
    )

    setup_playground_otel()
except Exception as _telemetry_err:  # noqa: BLE001 — telemetry is optional
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "playground telemetry disabled — /config/langfuse will report "
        "enabled: false: %s",
        _telemetry_err,
    )

app = FastAPI(
    title="Component Playground",
    description="Sealed inspection workbench for the 15 atomic components.",
    version="0.1.0",
)

# The frontend proxies requests through Next.js' rewrites, so in-cluster
# traffic never hits CORS. Kept permissive for anyone poking at the
# backend directly (curl, browser devtools) against the public IP.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def health() -> Response:
    """Cheap liveness probe for nginx / supervisor / smoke tests."""
    return Response(
        content="ok service=component-playground",
        media_type="text/plain",
    )


# Keep /health reachable even if the playground router's transitive
# imports (strands_evals, strands_agents.playground, …) fail — otherwise
# uvicorn can't construct `app`, supervisor's autorestart loop kicks in,
# and nginx has nothing to probe against. A guarded import means a deps
# drift gets logged once and surfaces via /health, not via a crash loop.
try:
    from playground import router as playground_router  # noqa: E402

    app.include_router(playground_router)
except ImportError as _imp_err:  # pragma: no cover - diagnostic path
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "playground router unavailable — /playground endpoints disabled: %s",
        _imp_err,
    )


# AG-UI transport — bridges the playground's RunStream to the AG-UI
# wire protocol so CopilotKit and other AG-UI consumers can subscribe
# to playground events without custom SSE parsing.
try:
    from agui_transport import add_playground_agui_endpoint  # noqa: E402

    add_playground_agui_endpoint(app, path="/agui")
except ImportError as _agui_err:  # pragma: no cover - diagnostic path
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "AG-UI transport unavailable — /agui endpoints disabled: %s",
        _agui_err,
    )


# AG-UI events bridge — forwards the legacy agui_events pub/sub bus
# (used by the production pipeline: agents, gatekeeper, recovery) to
# the AG-UI wire so CopilotKit can consume those events too.
try:
    from agui_bridge import add_agui_events_endpoint  # noqa: E402

    add_agui_events_endpoint(app, path="/agui")
except ImportError as _bridge_err:  # pragma: no cover - diagnostic path
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "AG-UI events bridge unavailable — /agui/events disabled: %s",
        _bridge_err,
    )


# Handy for `python -m playground_server` one-shots during debugging.
if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "playground_server:app",
        host="0.0.0.0",
        port=int(os.environ.get("PLAYGROUND_PORT", "8000")),
        reload=False,
    )
