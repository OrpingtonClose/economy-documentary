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
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import StreamingResponse

from dashboard import get_all_active_collectors, get_any_active_collector
from dashboard.event_store import get_all_runs, get_run_detail
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
