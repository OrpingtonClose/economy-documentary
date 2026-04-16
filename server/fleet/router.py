"""
Fleet REST endpoints — exposes the FleetCoordinator to VMAgents and dashboards.

Endpoints:
    POST /fleet/pull-work    — VMAgent requests next clip (pull model)
    POST /fleet/assign-clip  — Coordinator pushes priority clip to a worker
    POST /fleet/report       — VMAgent reports clip completion or failure
    GET  /fleet/status       — Full fleet status (queue, cost, patterns)
    GET  /fleet/queue        — Queue summary (depth, in-progress, completed)
"""

from __future__ import annotations

import logging
import re

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from fleet.coordinator import get_fleet_coordinator
from fleet.work_queue import QueuedClip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fleet", tags=["fleet"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class PullWorkRequest(BaseModel):
    """VMAgent requests next clip."""

    worker_id: str
    capabilities: list[str] = Field(default_factory=lambda: ["ltx"])
    status: dict[str, Any] = Field(default_factory=dict)


class PullWorkResponse(BaseModel):
    """Response with next clip (or null if none available)."""

    clip: Optional[dict[str, Any]] = None


class ReportRequest(BaseModel):
    """VMAgent reports clip result."""

    clip_id: str
    worker_id: str
    success: bool
    output_path: str = ""
    gen_time: float = 0.0
    qa_quality: str = ""
    qa_reason: str = ""
    error: str = ""
    error_category: str = "unknown"


class EnqueueRequest(BaseModel):
    """Enqueue clips into the work queue."""

    clips: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/pull-work", response_model=PullWorkResponse)
async def pull_work(req: PullWorkRequest) -> PullWorkResponse:
    """VMAgent requests the next clip to generate.

    Returns the highest-priority pending clip that hasn't failed on this
    worker, or ``{"clip": null}`` if no work is available.
    """
    coordinator = get_fleet_coordinator()
    if not coordinator:
        return PullWorkResponse(clip=None)

    clip = coordinator.pull_work(worker_id=req.worker_id)
    if clip is None:
        return PullWorkResponse(clip=None)

    return PullWorkResponse(clip={
        "clip_id": clip.clip_id,
        "scene_num": clip.scene_num,
        "phrase_idx": clip.phrase_idx,
        "prompt": clip.prompt,
        "negative_prompt": clip.negative_prompt,
        "duration": clip.duration,
        "lora_id": clip.lora_id,
        "lora_weight": clip.lora_weight,
        "priority": clip.priority,
        "attempt": clip.attempts,
    })


@router.post("/report")
async def report_result(req: ReportRequest) -> dict[str, str]:
    """VMAgent reports clip generation result (success or failure)."""
    coordinator = get_fleet_coordinator()
    if not coordinator:
        raise HTTPException(status_code=503, detail="Fleet coordinator not active")

    if req.success:
        coordinator.report_completed(
            clip_id=req.clip_id,
            output_path=req.output_path,
            gen_time=req.gen_time,
            qa_quality=req.qa_quality,
            qa_reason=req.qa_reason,
            worker_id=req.worker_id,
        )
        return {"status": "ok", "action": "completed"}
    else:
        coordinator.report_failed(
            clip_id=req.clip_id,
            worker_id=req.worker_id,
            error=req.error,
            category=req.error_category,
        )
        return {"status": "ok", "action": "failed_requeued"}


@router.post("/enqueue")
async def enqueue_clips(req: EnqueueRequest) -> dict[str, Any]:
    """Enqueue clips into the work queue (called by production callback)."""
    coordinator = get_fleet_coordinator()
    if not coordinator:
        raise HTTPException(status_code=503, detail="Fleet coordinator not active")

    queued_clips = []
    for c in req.clips:
        queued_clips.append(QueuedClip(
            clip_id=c.get("clip_id", f"scene_{int(re.sub(r'[^0-9]', '', str(c.get('scene_num', 0))) or 0):03d}_phrase_{int(re.sub(r'[^0-9]', '', str(c.get('phrase_idx', 0))) or 0):03d}"),
            scene_num=c.get("scene_num", 0),
            phrase_idx=c.get("phrase_idx", 0),
            prompt=c.get("prompt", ""),
            negative_prompt=c.get("negative_prompt", ""),
            duration=c.get("duration", 5.0),
            lora_id=c.get("lora_id", "documentary-realism"),
            lora_weight=c.get("lora_weight", 0.7),
            priority=c.get("priority", 0),
        ))

    count = coordinator.enqueue_clips(queued_clips)
    return {"status": "ok", "enqueued": count, "total": len(req.clips)}


@router.get("/status")
async def fleet_status() -> dict[str, Any]:
    """Full fleet status: queue, cost, systemic patterns, pause state."""
    coordinator = get_fleet_coordinator()
    if not coordinator:
        return {
            "active": False,
            "message": "Fleet coordinator not active (single-worker mode)",
        }

    summary = coordinator.get_summary()
    summary["active"] = True
    summary["cost"] = coordinator.cost_tracker.summary()
    return summary


@router.get("/queue")
async def queue_status() -> dict[str, Any]:
    """Queue summary: depth, in-progress, completed, dead-lettered."""
    coordinator = get_fleet_coordinator()
    if not coordinator:
        return {"active": False}

    return {
        "active": True,
        **coordinator.queue.get_summary(),
    }


@router.post("/pause")
async def pause_fleet(reason: str = "manual pause") -> dict[str, str]:
    """Pause the fleet (stop dispatching and provisioning)."""
    coordinator = get_fleet_coordinator()
    if not coordinator:
        raise HTTPException(status_code=503, detail="Fleet coordinator not active")
    coordinator._pause(reason)
    return {"status": "paused", "reason": reason}


@router.post("/resume")
async def resume_fleet() -> dict[str, str]:
    """Resume the fleet after a pause."""
    coordinator = get_fleet_coordinator()
    if not coordinator:
        raise HTTPException(status_code=503, detail="Fleet coordinator not active")
    coordinator.resume()
    return {"status": "resumed"}
