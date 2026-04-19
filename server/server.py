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

UI-07 (run persistence + reconnect):
- Each POST / gets a stable run id (``run-<hex>``) unless the request
  includes ``X-Pipeline-Run-Id`` of an existing run + ``Last-Event-ID``,
  in which case the endpoint runs in **resume mode** and replays
  buffered events instead of starting a new agent run.
- Every SSE chunk written to the wire also goes into a per-run ring
  buffer (see :mod:`run_registry`) tagged with a monotonic ``id:`` so
  clients can resume with ``Last-Event-ID``.
- ``GET /api/current-run`` exposes the latest run id for URL hydration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
from dashboard import (
    get_all_active_collectors,
    remove_collector,
    set_active_collector,
)
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
from run_registry import get_run_registry

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


def _parse_last_event_id(raw: Optional[str]) -> int:
    """Parse the ``Last-Event-ID`` header into an int (0 on failure)."""
    if not raw:
        return 0
    try:
        return max(0, int(raw.strip()))
    except (ValueError, AttributeError):
        return 0


def _tagged(seq: int, sse_text: str) -> str:
    """Prepend the SSE ``id:`` field to an already-encoded event."""
    return f"id: {seq}\n{sse_text}"


def _buffer_overflow_event(run_id: str, last_seq: int) -> str:
    """Return an SSE chunk announcing that some events were evicted.

    Shipped as an AG-UI CustomEvent so the frontend reducer can route
    it the same way as live pipeline events.
    """
    payload = {
        "type": "buffer_overflow",
        "run_id": run_id,
        "last_seq": last_seq,
        "message": (
            "Some events were evicted from the server buffer before you "
            "reconnected. The dashboard is refetching the snapshot."
        ),
        "timestamp": time.time(),
    }
    custom = CustomEvent(
        type=EventType.CUSTOM,
        name="buffer_overflow",
        value=payload,
    )
    # Use a fresh encoder so we don't depend on Accept headers here.
    return EventEncoder().encode(custom)


def _run_started_event(run_id: str) -> str:
    """Return an SSE chunk carrying the pipeline run id.

    Emitted as the very first event on every fresh POST /. The frontend
    uses this to stamp ``?run={id}`` into the URL so refresh preserves
    the session.
    """
    payload = {
        "type": "run_started",
        "run_id": run_id,
        "timestamp": time.time(),
    }
    custom = CustomEvent(
        type=EventType.CUSTOM,
        name="run_started",
        value=payload,
    )
    return EventEncoder().encode(custom)


async def _replay_stream(run_id: str, last_event_id: int):
    """Generator yielding buffered SSE chunks > ``last_event_id``.

    Yields a ``buffer_overflow`` custom event first if the client's
    Last-Event-ID sits behind the oldest entry still in the ring buffer.
    Emits a final ``run_started``-style marker so the client can pick up
    the current seq id even when no buffered events exceed the cursor.
    """
    registry = get_run_registry()
    events, overflow, latest = registry.replay(run_id, last_event_id)
    if overflow:
        # seq 0 is a safe sentinel because every real event is >= 1.
        yield _tagged(0, _buffer_overflow_event(run_id, latest))
    for seq, sse_text in events:
        yield _tagged(seq, sse_text)
    # Make the replay flush explicit so the client can close the resume
    # fetch once everything has been drained.
    replay_done = CustomEvent(
        type=EventType.CUSTOM,
        name="replay_done",
        value={
            "type": "replay_done",
            "run_id": run_id,
            "last_seq": latest,
            "replayed": len(events),
            "timestamp": time.time(),
        },
    )
    yield f": end of replay\n{EventEncoder().encode(replay_done)}"


@app.post("/")
async def unified_agui_endpoint(request: Request):
    """AG-UI endpoint with unified SSE stream.

    Merges three event sources into one SSE connection:
    1. AG-UI protocol events from the ADK agent (text, tool calls, state)
    2. Pipeline events from emit_agui_event() (artifacts, gatekeeper, escalations)
    3. Heartbeat comments every few seconds to keep the connection alive

    This prevents the browser/proxy from closing the connection during long
    deterministic operations (TTS generation, video rendering) that can take
    10+ minutes per stage.

    UI-07: if the request carries ``X-Pipeline-Run-Id`` for a run we know
    about, we run in **resume mode** and replay the buffered events
    whose seq is greater than the caller's ``Last-Event-ID`` header.
    """
    registry = get_run_registry()
    accept_header = request.headers.get("accept")

    run_id: str = getattr(request.state, "run_id", None) or registry.create()
    is_resume: bool = getattr(request.state, "is_resume", False)
    last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))

    if is_resume:
        # Replay-only mode. Do NOT start a new agent run; the original
        # POST owns the agent lifecycle. We just drain the ring buffer.
        response = StreamingResponse(
            _replay_stream(run_id, last_event_id),
            media_type=EventEncoder(accept=accept_header).get_content_type(),
        )
        response.headers["X-Pipeline-Run-Id"] = run_id
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    # Fresh run. Parse the AG-UI input body here (the middleware already
    # minted the run id).
    body_bytes = await request.body()
    try:
        body_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except json.JSONDecodeError:
        body_json = {}
    try:
        input_data = RunAgentInput.model_validate(body_json)
    except Exception as exc:  # pragma: no cover -- validation mirrors ag_ui
        return JSONResponse(
            {"error": "invalid AG-UI input", "detail": str(exc)},
            status_code=422,
        )

    encoder = EventEncoder(accept=accept_header)

    # Subscribe to pipeline events (artifacts, gatekeeper, escalations)
    pipeline_queue = subscribe_agui_events()

    async def event_generator():
        # Announce the run id BEFORE any agent/pipeline traffic so the
        # frontend can stamp the URL immediately (UI-07a).
        run_started_sse = _run_started_event(run_id)
        seq = registry.append(run_id, run_started_sse)
        yield _tagged(seq, run_started_sse)

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
                        sse = encoder.encode(err_event)
                        seq = registry.append(run_id, sse)
                        yield _tagged(seq, sse)
                    except Exception:
                        fallback = 'event: error\ndata: {"error": "Agent execution failed"}\n\n'
                        seq = registry.append(run_id, fallback)
                        yield _tagged(seq, fallback)
                    break
                elif kind == "agent":
                    try:
                        sse = encoder.encode(payload)
                        seq = registry.append(run_id, sse)
                        yield _tagged(seq, sse)
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
                        sse = encoder.encode(custom)
                        seq = registry.append(run_id, sse)
                        yield _tagged(seq, sse)
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
                    sse = encoder.encode(custom)
                    seq = registry.append(run_id, sse)
                    yield _tagged(seq, sse)
                except Exception:
                    pass

        finally:
            pipe_task.cancel()
            if not agent_task.done():
                agent_task.cancel()
            unsubscribe_agui_events(pipeline_queue)

    response = StreamingResponse(
        event_generator(),
        media_type=encoder.get_content_type(),
    )
    response.headers["X-Pipeline-Run-Id"] = run_id
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


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
