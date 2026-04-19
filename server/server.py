"""FastAPI + AG-UI server for the documentary pipeline.

Provides:
- POST / -- AG-UI endpoint for CopilotKit frontend (unified SSE stream)
- /dashboard/* -- pipeline REST endpoints (snapshots, runs, reports)
- /agui/* -- artifact, escalation, and feedback REST endpoints
- Request logging middleware
- Pipeline collector middleware for dashboard integration

All real-time events (pipeline progress, artifacts, gatekeeper, escalations)
flow through the single CopilotKit SSE stream at POST /.  There are no
separate SSE channels — the chat connection IS the pipeline connection.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, StreamingResponse

load_dotenv()

from ag_ui.core import CustomEvent, EventType, RunAgentInput
from ag_ui.encoder import EventEncoder
from ag_ui_adk import ADKAgent
from google.adk.apps import App

from agents.model_config import ADK_MODEL_NAME
from agents.pipeline import pipeline_agent
# Callbacks are wired directly into individual Agent sub-agents
# (scenario_director.py, visual_director.py) — not at the AG-UI level.
# See: before_model, after_model, before_tool, after_tool callbacks.
from dashboard import remove_collector, set_active_collector
from dashboard.collector import PipelineCollector
from dashboard.event_store import init_db, insert_run, finalize_run, insert_snapshot
from agui import (
    router as agui_router,
    api_router as agui_api_router,
    subscribe_agui_events,
    unsubscribe_agui_events,
)
from dashboard.sse import router as dashboard_router
from dashboard_directives import router as dashboard_directives_router
from fleet.router import router as fleet_router
from plugins import build_plugins, setup_otel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def _validate_env() -> None:
    """Validate required environment variables at startup."""
    model = ADK_MODEL_NAME
    if not model:
        raise ValueError("ADK_MODEL environment variable is required")

    # Check for at least one API key
    has_google = bool(os.environ.get("GOOGLE_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_api_base = bool(os.environ.get("OPENAI_API_BASE"))

    if not (has_google or has_openai or has_api_base):
        logger.warning(
            "No API keys configured. Set GOOGLE_API_KEY for Gemini "
            "or OPENAI_API_KEY + OPENAI_API_BASE for LiteLLM routing."
        )

    logger.info(
        "Model configuration: ADK_MODEL=%s, Gemini=%s, LiteLLM=%s",
        model,
        "yes" if has_google else "no",
        "yes" if (has_openai or has_api_base) else "no",
    )


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


class AGUIRunCollectorMiddleware(BaseHTTPMiddleware):
    """Create and manage PipelineCollector for each AG-UI request."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path != "/" or request.method != "POST":
            return await call_next(request)

        run_id = f"run_{uuid.uuid4().hex[:8]}"
        collector = PipelineCollector(run_id=run_id)
        set_active_collector(collector)
        insert_run(run_id)

        try:
            response = await call_next(request)

            # For SSE responses, we need to wrap the body iterator
            if hasattr(response, "body_iterator"):
                original_iterator = response.body_iterator

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

                response.body_iterator = wrapped_iterator()

            return response
        except Exception:
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
    """Application lifespan: setup OTel, validate env, init DB, fleet coordinator."""
    _validate_env()
    setup_otel()
    init_db()

    # Start fleet coordinator if FLEET_MODE is enabled
    _fleet_coordinator = None
    fleet_mode = os.environ.get("FLEET_MODE", "").strip().lower() in ("1", "true")
    if fleet_mode:
        try:
            from fleet.coordinator import create_fleet_coordinator
            budget = float(os.environ.get("PRODUCTION_BUDGET", "0"))
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

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


# AG-UI endpoint -- custom wrapper that merges pipeline events + heartbeats
# into the CopilotKit SSE stream so the connection never goes idle.
_adk_app = App(
    name="documentary_pipeline",
    root_agent=pipeline_agent,
    plugins=build_plugins(),
)
adk_agent = ADKAgent.from_app(
    app=_adk_app,
)

_HEARTBEAT_INTERVAL = 5  # seconds between SSE heartbeats during idle periods


@app.post("/")
async def unified_agui_endpoint(input_data: RunAgentInput, request: Request):
    """AG-UI endpoint with unified SSE stream.

    Merges three event sources into one SSE connection:
    1. AG-UI protocol events from the ADK agent (text, tool calls, state)
    2. Pipeline events from emit_agui_event() (artifacts, gatekeeper, escalations)
    3. Heartbeat comments every few seconds to keep the connection alive

    This prevents the browser/proxy from closing the connection during long
    deterministic operations (TTS generation, video rendering) that can take
    10+ minutes per stage.
    """
    accept_header = request.headers.get("accept")
    encoder = EventEncoder(accept=accept_header)

    # Subscribe to pipeline events (artifacts, gatekeeper, escalations)
    pipeline_queue = subscribe_agui_events()

    async def event_generator():
        merged: asyncio.Queue = asyncio.Queue()

        async def agent_reader():
            """Read AG-UI events from the ADK agent and forward to merged queue."""
            try:
                async for event in adk_agent.run(input_data):
                    await merged.put(("agent", event))
            except Exception as exc:
                await merged.put(("agent_error", exc))
            finally:
                await merged.put(("agent_done", None))

        async def pipeline_reader():
            """Poll the thread-safe deque for pipeline events."""
            try:
                while True:
                    if pipeline_queue:
                        event = pipeline_queue.popleft()
                        await merged.put(("pipeline", event))
                    else:
                        await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                pass

        agent_task = asyncio.create_task(agent_reader())
        pipe_task = asyncio.create_task(pipeline_reader())

        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        merged.get(), timeout=_HEARTBEAT_INTERVAL
                    )
                except asyncio.TimeoutError:
                    # No events for a while — send SSE comment to keep alive
                    yield ": heartbeat\n\n"
                    continue

                if kind == "agent_done":
                    break
                elif kind == "agent_error":
                    logger.error("ADK agent error in unified stream: %s", payload)
                    # Save trace capture on error path (pipeline cleanup may not run)
                    try:
                        from orchestrator.trace_capture import get_trace_capture
                        _tc = get_trace_capture()
                        _tc.end_run(summary=f"Pipeline error: {str(payload)[:200]}")
                        _tc_path = _tc.save()
                        logger.info("Trace captured on error path: %s", _tc_path)
                    except Exception as _tc_err:
                        logger.debug("Trace capture on error skipped: %s", _tc_err)
                    from ag_ui.core import RunErrorEvent
                    err_event = RunErrorEvent(
                        type=EventType.RUN_ERROR,
                        message=f"Agent execution failed: {payload}",
                        code="AGENT_ERROR",
                    )
                    try:
                        yield encoder.encode(err_event)
                    except Exception:
                        yield 'event: error\ndata: {"error": "Agent execution failed"}\n\n'
                    break
                elif kind == "agent":
                    try:
                        yield encoder.encode(payload)
                    except Exception as enc_err:
                        logger.error("Event encoding error: %s", enc_err)
                elif kind == "pipeline":
                    # Forward pipeline events as AG-UI CustomEvents
                    custom = CustomEvent(
                        type=EventType.CUSTOM,
                        name="pipeline_event",
                        value=payload,
                    )
                    try:
                        yield encoder.encode(custom)
                    except Exception as enc_err:
                        logger.error("Pipeline event encoding error: %s", enc_err)

            # Drain remaining pipeline events after agent finishes
            while pipeline_queue:
                event = pipeline_queue.popleft()
                custom = CustomEvent(
                    type=EventType.CUSTOM,
                    name="pipeline_event",
                    value=event,
                )
                try:
                    yield encoder.encode(custom)
                except Exception:
                    pass

        finally:
            pipe_task.cancel()
            if not agent_task.done():
                agent_task.cancel()
            unsubscribe_agui_events(pipeline_queue)

    return StreamingResponse(
        event_generator(),
        media_type=encoder.get_content_type(),
    )


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "model": ADK_MODEL_NAME,
        "version": "0.1.0",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
