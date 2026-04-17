"""FastAPI server for the documentary pipeline.

Provides:
- POST /run -- pipeline execution endpoint (SSE stream)
- /dashboard/* -- pipeline REST endpoints (snapshots, runs, reports)
- /health -- health check
- Request logging middleware

All real-time events flow through the SSE stream at POST /run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, StreamingResponse

load_dotenv()

from dashboard import remove_collector, set_active_collector
from dashboard.collector import PipelineCollector
from dashboard.event_store import init_db, insert_run, finalize_run, insert_snapshot
from dashboard.sse import router as dashboard_router
from fleet.router import router as fleet_router

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def _validate_env() -> None:
    """Validate required environment variables at startup."""
    model = os.environ.get("ADK_MODEL", "")
    if not model:
        logger.warning("ADK_MODEL environment variable is not set")

    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_api_base = bool(os.environ.get("OPENAI_API_BASE"))

    if not (has_openai or has_api_base):
        logger.warning(
            "No API keys configured. Set OPENAI_API_KEY + OPENAI_API_BASE for LiteLLM routing."
        )

    logger.info(
        "Model configuration: ADK_MODEL=%s, LiteLLM=%s",
        model or "(default)",
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: validate env, init DB, fleet coordinator."""
    _validate_env()
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

    logger.info("Documentary pipeline server started (Strands SDK)")
    yield

    # Shutdown fleet coordinator
    if _fleet_coordinator:
        _fleet_coordinator.shutdown()
        from fleet.coordinator import reset_fleet_coordinator
        reset_fleet_coordinator()
    logger.info("Documentary pipeline server shutting down")


app = FastAPI(
    title="Documentary Pipeline",
    description="ADHD-friendly AI documentary pipeline with Strands Agents SDK",
    version="0.2.0",
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

# Middleware
app.add_middleware(RequestLoggingMiddleware)

# Dashboard routes
app.include_router(dashboard_router)

_HEARTBEAT_INTERVAL = 5  # seconds between SSE heartbeats

# Fleet coordination routes (pull-work, report, queue status)
app.include_router(fleet_router)


@app.post("/run")
async def run_pipeline_endpoint(request: Request):
    """Pipeline execution endpoint with SSE stream.

    Accepts JSON body with:
    - topic (str): Documentary topic
    - corpus_path (str): Path to research corpus
    - language (str, optional): Language mode (default: dual_ru_en)
    - quick_test (bool, optional): Quick test mode

    Returns an SSE stream with pipeline progress events.
    """
    body = await request.json()
    topic = body.get("topic", "")
    corpus_path = body.get("corpus_path", "")
    language = body.get("language", "dual_ru_en")
    quick_test = body.get("quick_test", False)

    if not topic:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "topic is required"}, status_code=400)

    run_id = f"run_{uuid.uuid4().hex[:8]}"
    collector = PipelineCollector(run_id=run_id, topic=topic)
    set_active_collector(collector)
    insert_run(run_id)

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'run_start', 'run_id': run_id, 'topic': topic})}\n\n"

            # Run pipeline in a thread to avoid blocking
            from run_pipeline import run_pipeline
            loop = asyncio.get_event_loop()

            # quick_test flag is passed directly to run_pipeline() which
            # propagates it via initial_state["quick_test"]. We do NOT set
            # process-global env vars here — that would race between
            # concurrent requests.
            import contextvars
            ctx = contextvars.copy_context()
            pipeline_future = loop.run_in_executor(
                None,
                lambda: ctx.run(
                    run_pipeline,
                    topic=topic,
                    corpus_path=corpus_path,
                    language=language,
                    quick_test=quick_test,
                ),
            )

            # Send heartbeats while waiting so proxies/browsers
            # don't drop the idle SSE connection during long runs.
            while not pipeline_future.done():
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(pipeline_future),
                        timeout=_HEARTBEAT_INTERVAL,
                    )
                    break
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
            else:
                result = pipeline_future.result()

            # Send result
            serializable = {}
            if result and isinstance(result, dict):
                for k, v in result.items():
                    try:
                        json.dumps(v)
                        serializable[k] = v
                    except (TypeError, ValueError):
                        serializable[k] = str(v)

            yield f"data: {json.dumps({'type': 'run_complete', 'run_id': run_id, 'result': serializable})}\n\n"

        except Exception as exc:
            logger.exception("Pipeline execution failed")
            yield f"data: {json.dumps({'type': 'run_error', 'run_id': run_id, 'error': str(exc)})}\n\n"
        finally:
            data = collector.finalize()
            finalize_run(run_id, status=data.get("status", "unknown"), metadata=data)
            insert_snapshot(run_id, data)
            remove_collector(run_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
    )


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "model": os.environ.get("ADK_MODEL", "(default)"),
        "framework": "strands-agents",
        "version": "0.2.0",
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
