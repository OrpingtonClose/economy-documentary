"""
FastAPI + AG-UI server for the documentary pipeline.

Ported from MiroThinker. Provides:
- POST / -- AG-UI endpoint for CopilotKit frontend
- /dashboard/* -- real-time pipeline dashboard endpoints
- Request logging middleware
- Pipeline collector middleware for dashboard integration
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

load_dotenv()

from ag_ui_adk import ADKAgent, add_adk_fastapi_endpoint

from agents.model_config import ADK_MODEL_NAME
from agents.pipeline import pipeline_agent
# Callbacks are wired directly into individual Agent sub-agents
# (scenario_director.py, visual_director.py) — not at the AG-UI level.
# See: before_model, after_model, before_tool, after_tool callbacks.
from dashboard import remove_collector, set_active_collector
from dashboard.collector import PipelineCollector
from dashboard.event_store import init_db, insert_run, finalize_run, insert_snapshot
from dashboard.sse import router as dashboard_router
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
            remove_collector(run_id)
            raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: setup OTel, validate env, init DB."""
    _validate_env()
    setup_otel()
    init_db()
    logger.info("Documentary pipeline server started")
    yield
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


# AG-UI endpoint -- uses add_adk_fastapi_endpoint which registers POST /
adk_agent = ADKAgent(
    adk_agent=pipeline_agent,
    app_name="documentary-pipeline",
)
add_adk_fastapi_endpoint(app, adk_agent, path="/")


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
