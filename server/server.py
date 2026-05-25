"""FastAPI server for the documentary pipeline.

Provides:
- /playground/* -- Strands pipeline playground (active pipeline)
- /dashboard/* -- pipeline REST endpoints (snapshots, runs, reports)
- /agui/* -- artifact, escalation, and feedback REST endpoints
- /fleet/* -- Vast.ai VM fleet management
- Request logging middleware
- Pipeline collector middleware for dashboard integration
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# AG-UI / CopilotKit imports removed — ADK pipeline deleted.
# The playground router (Strands) is the active pipeline path.
from dashboard import (  # noqa: E402
    get_all_active_collectors,
    remove_collector,
    set_active_collector,
)
from dashboard.collector import PipelineCollector  # noqa: E402
from dashboard.event_store import init_db, insert_run, finalize_run, insert_snapshot  # noqa: E402
from agui import (  # noqa: E402
    router as agui_router,
    api_router as agui_api_router,
)
from dashboard.sse import router as dashboard_router  # noqa: E402
from dashboard_directives import router as dashboard_directives_router  # noqa: E402
from fleet.router import router as fleet_router  # noqa: E402
from playground import router as playground_router  # noqa: E402
# Strands pipeline archived — only v4 execution path remains
# plugins removed — ADK pipeline deleted
from run_registry import get_run_registry  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def _validate_env() -> None:
    """Validate required configuration at startup."""
    pass


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log request method, path, status, and duration."""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start
        logger.info(
            "%s %s -> %d (%.2fs)",
            request.method,
            request.url.path,
            response.status_code,
            duration,
        )
        return response


def _resolve_resume(request: Request) -> tuple[Optional[str], bool]:
    """Return ``(run_id, is_resume)`` based on the resume protocol headers.

    Resume mode is engaged only when the client supplies an
    ``X-Pipeline-Run-Id`` header that matches a run the registry knows
    about. Without that, we're starting a fresh run and the caller is
    expected to mint a new id.
    """
    header_run_id = request.headers.get("x-pipeline-run-id") or request.headers.get(
        "X-Pipeline-Run-Id"
    )
    if not header_run_id:
        return None, False
    if not get_run_registry().exists(header_run_id):
        return None, False
    return header_run_id, True


class AGUIRunCollectorMiddleware(BaseHTTPMiddleware):
    """Create and manage PipelineCollector for each AG-UI request.

    Resume-aware (UI-07): if the request carries an existing run id via
    ``X-Pipeline-Run-Id``, we re-attach the collector from the registry
    and skip create/finalize so replay connections don't trample the
    original run's lifecycle records.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path != "/" or request.method != "POST":
            return await call_next(request)

        registry = get_run_registry()
        resume_run_id, is_resume = _resolve_resume(request)

        if is_resume and resume_run_id is not None:
            # Attach the existing collector to the async ContextVar so
            # any callbacks that fire on this connection write to the
            # right run. Finalize is owned by the original connection;
            # resumption must not mutate run state.
            request.state.run_id = resume_run_id
            request.state.is_resume = True
            existing = get_all_active_collectors().get(resume_run_id)
            if existing is not None:
                set_active_collector(existing)
            return await call_next(request)

        run_id = registry.create()
        collector = PipelineCollector(run_id=run_id)
        set_active_collector(collector)
        insert_run(run_id)
        request.state.run_id = run_id
        request.state.is_resume = False

        try:
            response = await call_next(request)

            # For SSE responses, we need to wrap the body iterator
            original_iterator = getattr(response, "body_iterator", None)
            if original_iterator is not None:
                async def wrapped_iterator():
                    try:
                        async for chunk in original_iterator:
                            yield chunk
                    finally:
                        data = collector.finalize()
                        finalize_run(run_id, status=data["status"], metadata=data)
                        insert_snapshot(run_id, data)
                        remove_collector(run_id)
                        logger.info("Pipeline run %s finalized", run_id)

                response.body_iterator = wrapped_iterator()  # type: ignore[attr-defined]

            return response
        except Exception as exc:
            from maintainer import notify_maintainer
            notify_maintainer("server_pipeline_error", str(exc), {"run_id": run_id})
            collector.finalize(status="error")
            finalize_run(run_id, status="error")
            # Do NOT remove_collector on error — keep it in the registry
            # so /dashboard/latest still returns the crash state + recent
            # events instead of going blank ("Waiting for pipeline...").
            # The collector will be replaced when the next run starts.
            logger.error("Pipeline run %s crashed — collector retained for dashboard", run_id)
            raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: validate env, init DB, fleet coordinator."""
    _validate_env()
    init_db()

    # Start fleet coordinator if FLEET_MODE is enabled
    _fleet_coordinator = None
    fleet_mode = False
    if fleet_mode:
        try:
            from fleet.coordinator import create_fleet_coordinator
            budget = 0.0
            _fleet_coordinator = create_fleet_coordinator(budget_ceiling=budget)
            _fleet_coordinator.start()
            logger.info(
                "Fleet coordinator started at server level (budget=$%.2f)",
                budget,
            )
        except Exception as e:
            logger.warning("Fleet coordinator failed to start: %s", e)

    # Start reasoning digest engine (background thread that batch-processes
    # raw traces into concise summaries for the frontend observer)
    from plugins.reasoning_digest import get_digest_engine
    _digest_engine = get_digest_engine()

    logger.info("Documentary pipeline server started")
    yield

    # Shutdown digest engine
    _digest_engine.stop()

    # Shutdown fleet coordinator
    if _fleet_coordinator:
        _fleet_coordinator.shutdown()
        from fleet.coordinator import reset_fleet_coordinator
        reset_fleet_coordinator()
    logger.info("Documentary pipeline server shutting down")


app = FastAPI(
    title="Documentary Pipeline",
    description="ADHD-friendly AI documentary pipeline with Google ADK + AG-UI",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS -- explicitly expose the Last-Event-ID machinery so browser-side
# replay clients can read id offsets and the buffer_overflow marker from
# cross-origin responses.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Pipeline-Run-Id", "X-Pipeline-Last-Event-Id"],
)

# Middleware stack (order matters: last added = first executed)
app.add_middleware(AGUIRunCollectorMiddleware)
app.add_middleware(RequestLoggingMiddleware)

# Dashboard routes
app.include_router(dashboard_router)

# AG-UI routes (artifact feedback, escalations, regeneration)
app.include_router(agui_router)

# ARCH-H5 (issue #160): dedicated /api/reasoning_digest_stream SSE endpoint
# for the rule-based reasoning digest feed.
app.include_router(agui_api_router)

# ARCH-H4 (#159): dashboard halt button + directive injection
app.include_router(dashboard_directives_router)

# Fleet coordination routes (pull-work, report, queue status)
app.include_router(fleet_router)

# Component Playground — read-only catalog for the standalone
# frontend-playground workbench (docs/strands-migration/plans/
# component-playground.md). Additive; does not touch /agui.
app.include_router(playground_router)

# Agent intervention removed — only v4 pipeline execution path


# ADK AG-UI endpoint removed — CopilotKit frontend no longer served.
# The Strands playground at /playground/* is the active pipeline.


@app.get("/api/current-run")
async def current_run():
    """Return the id of the latest pipeline run (UI-07a).

    Frontend calls this on load to confirm the ``?run=`` query param in
    the URL still matches a run the server knows about. If the requested
    run is gone (server restarted, etc.) the client falls back to the
    "await topic" initial state.
    """
    registry = get_run_registry()
    rid = registry.latest()
    if not rid:
        return {"run_id": None, "exists": False, "latest_seq": 0}
    return {
        "run_id": rid,
        "exists": True,
        "latest_seq": registry.latest_seq(rid),
    }


@app.get("/api/runs/{run_id}/exists")
async def run_exists(run_id: str):
    """Cheap existence probe used by the resume handshake."""
    registry = get_run_registry()
    exists = registry.exists(run_id)
    return {
        "run_id": run_id,
        "exists": exists,
        "latest_seq": registry.latest_seq(run_id) if exists else 0,
    }


@app.get("/")
async def health():
    """Health check endpoint."""
    return Response(
        content="ok model=deepseek-v4-flash version=0.1.0",
        media_type="text/plain",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
