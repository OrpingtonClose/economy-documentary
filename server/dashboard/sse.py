"""
REST endpoints for pipeline dashboard.

Provides:
- /dashboard/latest -- latest snapshot (polled by frontend)
- /dashboard/runs -- list of all runs
- /dashboard/runs/{run_id} -- detail for a specific run
- /dashboard/html/{run_id} -- HTML report for a run

Real-time streaming is handled by the unified CopilotKit SSE endpoint
(POST /) in server.py.  There is no separate /dashboard/stream SSE.

Issues #66 and #68 (dashboard blind after scenario / SSE drops during
audio phase):

The original /ingest handler only accepted phase_start/phase_end and
tool_start/tool_end events, which meant per-scene audio clip generation
never hit the dashboard (it was one tool call that looked like a single
long-running operation).  This module now accepts ``stage_event`` pings
with structured ``scene_id``, ``stage``, and ``status`` fields so every
major pipeline stage — scenario, audio, video, assembly, gatekeeper —
emits at least one event per scene.  The audio agent in particular now
pings once per clip so the dashboard never goes dark.

The companion helper ``emit_stage_event()`` is importable from other
modules so agents can emit without hand-rolling the HTTP payload.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional
from urllib import request as _urllib_request

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dashboard import get_all_active_collectors, get_any_active_collector, set_active_collector
from dashboard.collector import PipelineCollector
from dashboard.event_store import get_all_runs, get_run_detail, insert_run
from dashboard.html_report import generate_dashboard_html

logger = logging.getLogger(__name__)

# Stages that the dashboard expects at least one event from per run.
# Used by the audit check at /dashboard/coverage.  Keep this aligned with
# the pipeline stage names emitted by callbacks/deterministic_steps.py.
KNOWN_PIPELINE_STAGES = (
    "scenario",
    "visual_director",
    "audio",
    "video",
    "assembly",
    "gatekeeper",
    "finalize",
)


def emit_stage_event(
    run_id: str,
    stage: str,
    status: str,
    scene_id: Optional[str] = None,
    detail: str = "",
    dashboard_url: Optional[str] = None,
) -> bool:
    """Emit a structured stage event to the dashboard /ingest endpoint.

    Returns True on a successful POST, False on any failure (network
    error, missing dashboard URL, etc).  Callers should treat this as
    best-effort observability — never a fatal dependency.

    Issue #66/#68: every agent's major step should call this at least
    once with (scene_id, stage, status) so the dashboard can render
    progress granularity finer than the overall phase.
    """
    url = dashboard_url or os.environ.get(
        "DASHBOARD_URL", "http://127.0.0.1:8000"
    )
    payload = {
        "run_id": run_id,
        "event_type": "stage_event",
        "stage": stage,
        "status": status,
        "scene_id": scene_id or "",
        "detail": detail,
        "ts": time.time(),
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = _urllib_request.Request(
            f"{url.rstrip('/')}/dashboard/ingest",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urllib_request.urlopen(req, timeout=2) as resp:
            resp.read()
        return True
    except Exception as exc:
        logger.debug("emit_stage_event(%s/%s) failed: %s", stage, status, exc)
        return False

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


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
    elif event_type == "stage_event":
        # #66/#68: structured per-scene progress event.  Record the
        # human-readable detail so the dashboard's "Recent Events"
        # section provides anxiety-release during long operations.
        stage = body.get("stage", "unknown")
        status = body.get("status", "running")
        scene_id = body.get("scene_id", "")
        detail = body.get("detail", "")
        label = f"{stage}:{scene_id}" if scene_id else stage

        # Always record the structured stage event with detail text
        collector.stage_event(stage, status, scene_id, detail)

        # Also use phase plumbing so it shows up in phase counters
        if status in ("start", "started", "running", "in_progress"):
            collector.phase_start(label)
        elif status in ("complete", "completed", "ok", "done", "clip_done"):
            collector.phase_end(label, status="completed")
        elif status in ("error", "failed", "fail"):
            collector.phase_end(label, status="failed")
        elif status == "recovered":
            collector.phase_end(label, status="recovered")
        logger.debug(
            "Dashboard stage_event: run=%s stage=%s scene=%s status=%s detail=%s",
            run_id, stage, scene_id, status, detail[:80],
        )
    # heartbeat: just keeps the collector alive, no action needed

    return JSONResponse({"status": "ok", "run_id": run_id})


@router.get("/coverage/{run_id}")
async def dashboard_coverage(run_id: str):
    """Report which stages have emitted at least one event for a run.

    Issue #66/#68 audit: the pipeline should emit at least one event per
    ``KNOWN_PIPELINE_STAGES`` entry per run.  Gaps here mean the
    dashboard went blind for that stage — typically indicative of a
    blocking call in the same event loop as the SSE emitter.
    """
    collectors = get_all_active_collectors()
    collector = collectors.get(run_id)
    if collector is None:
        detail = get_run_detail(run_id)
        if not detail:
            return JSONResponse(
                {"error": f"Run {run_id} not found"}, status_code=404
            )
        events = detail.get("events", [])
    else:
        events = collector.to_report_dict().get("events", [])

    seen: set[str] = set()
    for ev in events:
        name = ev.get("name", "") or ev.get("tool", "")
        for stage in KNOWN_PIPELINE_STAGES:
            if name.startswith(stage):
                seen.add(stage)
                break

    missing = [s for s in KNOWN_PIPELINE_STAGES if s not in seen]
    return JSONResponse(
        {
            "run_id": run_id,
            "seen_stages": sorted(seen),
            "missing_stages": missing,
            "complete": not missing,
        }
    )


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
