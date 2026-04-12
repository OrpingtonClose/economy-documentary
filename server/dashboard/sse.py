"""
SSE and REST endpoints for real-time pipeline dashboard.

Ported from MiroThinker. Provides:
- /dashboard/stream -- SSE endpoint for live pipeline updates
- /dashboard/latest -- latest snapshot
- /dashboard/runs -- list of all runs
- /dashboard/runs/{run_id} -- detail for a specific run
- /dashboard/html/{run_id} -- HTML report for a run
"""

from __future__ import annotations

import asyncio
import json
import logging
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import StreamingResponse

from dashboard import get_all_active_collectors, get_any_active_collector, set_active_collector
from dashboard.collector import PipelineCollector
from dashboard.event_store import get_all_runs, get_run_detail, insert_run
from dashboard.html_report import generate_dashboard_html

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stream")
async def dashboard_stream():
    """SSE endpoint for live pipeline updates."""
    async def event_generator():
        while True:
            collector = get_any_active_collector()
            if collector:
                snapshot = collector.snapshot()
                yield f"data: {json.dumps(snapshot)}\n\n"
            else:
                yield f"data: {json.dumps({'status': 'idle'})}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/latest")
async def dashboard_latest():
    """Get the latest pipeline snapshot."""
    collector = get_any_active_collector()
    if collector:
        return JSONResponse(collector.snapshot())
    return JSONResponse({"status": "idle", "message": "No active pipeline"})


@router.get("/runs")
async def dashboard_runs():
    """List all pipeline runs from the event store."""
    runs = get_all_runs()
    return JSONResponse({"runs": runs})


@router.get("/runs/{run_id}")
async def dashboard_run_detail(run_id: str):
    """Get detail for a specific run."""
    detail = get_run_detail(run_id)
    if not detail:
        return JSONResponse(
            {"error": f"Run {run_id} not found"}, status_code=404
        )
    return JSONResponse(detail)


@router.get("/html/{run_id}", response_class=HTMLResponse)
async def dashboard_html(run_id: str):
    """Generate HTML report for a specific run."""
    # Try in-memory collector first
    collectors = get_all_active_collectors()
    if run_id in collectors:
        data = collectors[run_id].to_report_dict()
        return HTMLResponse(generate_dashboard_html(data))

    # Fall back to event store
    detail = get_run_detail(run_id)
    if not detail:
        return HTMLResponse(f"<h1>Run {run_id} not found</h1>", status_code=404)

    return HTMLResponse(generate_dashboard_html(detail))


@router.get("/active")
async def dashboard_active():
    """List all active pipeline collectors."""
    collectors = get_all_active_collectors()
    return JSONResponse(
        {
            "active": [
                {"run_id": c.run_id, "topic": c.topic, "status": c.status}
                for c in collectors.values()
            ]
        }
    )


@router.post("/ingest")
async def dashboard_ingest(request: Request):
    """Ingest a status update from an external pipeline runner.

    This bridges run_pipeline.py (separate process) with the dashboard
    SSE stream served by server.py.  The runner POSTs JSON snapshots
    here and the dashboard picks them up via get_any_active_collector().

    Accepted body fields:
        run_id:          str  — unique run identifier
        topic:           str  — documentary topic
        event_type:      str  — "phase_start", "phase_end", "tool_start",
                                "tool_end", "llm_start", "llm_end",
                                "heartbeat", "finalize"
        phase:           str  — phase name (for phase_start/phase_end)
        status:          str  — status string (for phase_end / finalize)
        tool_name:       str  — tool name (for tool_start/tool_end)
        agent:           str  — agent name
        args_summary:    str  — tool args summary
        duration:        float — tool duration
        result_chars:    int  — tool result chars
        estimated_tokens: int — LLM estimated tokens
        output_tokens:   int — LLM output tokens
    """
    body = await request.json()
    run_id = body.get("run_id", "external")
    topic = body.get("topic", "")
    event_type = body.get("event_type", "heartbeat")

    # Get or create collector for this run
    collectors = get_all_active_collectors()
    collector = collectors.get(run_id)
    if collector is None:
        collector = PipelineCollector(run_id=run_id, topic=topic)
        set_active_collector(collector)
        insert_run(run_id, topic=topic)
        logger.info("Dashboard ingest: created collector for run %s (topic=%s)", run_id, topic)

    # Update topic if provided
    if topic and not collector.topic:
        collector.topic = topic

    # Dispatch event
    if event_type == "phase_start":
        collector.phase_start(body.get("phase", "unknown"))
    elif event_type == "phase_end":
        collector.phase_end(body.get("phase", "unknown"), body.get("status", "completed"))
    elif event_type == "tool_start":
        collector.tool_start(
            body.get("tool_name", "unknown"),
            body.get("agent", "unknown"),
            body.get("args_summary", ""),
        )
    elif event_type == "tool_end":
        collector.tool_end(
            body.get("tool_name", "unknown"),
            body.get("agent", "unknown"),
            body.get("duration", 0.0),
            body.get("result_chars", 0),
        )
    elif event_type == "llm_start":
        collector.llm_start(body.get("agent", "unknown"), body.get("estimated_tokens", 0))
    elif event_type == "llm_end":
        collector.llm_end(
            body.get("agent", "unknown"),
            body.get("duration", 0.0),
            body.get("output_tokens", 0),
        )
    elif event_type == "finalize":
        collector.finalize(body.get("status", "completed"))
    # heartbeat: just keeps the collector alive, no action needed

    return JSONResponse({"status": "ok", "run_id": run_id})


@router.get("/infra")
async def dashboard_infra():
    """Return infra agent status: worker health, stage timing, escalations.

    This is the active monitoring counterpart to the passive contract
    checks.  Poll this endpoint (or the SSE stream) to see real-time
    worker health and any escalation events.
    """
    from infra_agent import get_infra_agent

    agent = get_infra_agent()
    if agent is None:
        return JSONResponse(
            {"status": "not_running", "message": "InfraAgent has not been started"},
            status_code=200,
        )
    return JSONResponse(agent.get_status())
