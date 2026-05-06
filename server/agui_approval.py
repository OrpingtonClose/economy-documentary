"""
AG-UI Approval & Gatekeeper Layer — extracted from agui.py

This module contains all approval gate and gatekeeper-related code:

* The sequential approval stage order (``_STAGE_ORDER``)
* Approval state reading/writing (via callbacks.approval_gate)
* ``GET /approval-state`` — read current approval state for all stages
* ``POST /approve`` — approve a pipeline stage, unlocking the next
* ``GET /gatekeeper/checks`` — all gatekeeper check results
* ``GET /gatekeeper/rejects`` — gatekeeper REJECT verdicts
* ``POST /gatekeeper/halt`` — user halts pipeline during gatekeeper window
* ``POST /regenerate`` — trigger regeneration at clip / scene / style level

Architecture:
    Pipeline callbacks → approval_gate → _read/_write_approval_state
    Frontend dashboard → POST /agui/approve → unlock next stage
    Gatekeeper → /agui/gatekeeper/* → visibility + intervention
    Human → POST /agui/regenerate → queue regeneration
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agui_events import emit_agui_event, FeedbackStore, get_feedback_store, FeedbackType, HumanFeedback, _store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agui", tags=["agui"])

# Full sequence of human-in-the-loop gates. "audio" sits between
# "scenario" and "prompts" -- pipeline.py's narration stage calls
# wait_for_approval("audio") before handing off to the visual director,
# so UI-03 (#188) needs it listed here so /agui/approve accepts it when
# the inline approval card's Approve button posts {stage: "audio"}.
_STAGE_ORDER = ["scenario", "audio", "prompts", "clips", "timeline", "assembly"]


@router.get("/approval-state")
async def get_approval_state():
    """Return current approval state for all stages."""
    from callbacks.approval_gate import _read_approval_state
    state = _read_approval_state()
    return JSONResponse({"state": state, "stage_order": _STAGE_ORDER})


@router.post("/approve")
async def approve_stage(request: Request):
    """Approve a pipeline stage, unlocking the next one.

    Body: {"stage": "scenario" | "prompts" | "clips" | "timeline"}
    """
    body = await request.json()
    stage = body.get("stage", "")
    if stage not in _STAGE_ORDER:
        return JSONResponse(
            {"error": f"Invalid stage: {stage}. Must be one of {_STAGE_ORDER}"},
            status_code=400,
        )
    from callbacks.approval_gate import _read_approval_state, _write_approval_state
    state = _read_approval_state()
    state[stage] = {"approved": True, "timestamp": time.time()}
    _write_approval_state(state)
    logger.info("Stage '%s' approved", stage)
    # ARCH-H5 (issue #160): gate_close digest
    try:
        from dashboard.reasoning_digest import emit_digest
        emit_digest(
            None,
            "gate_close",
            {"stage": stage, "decision": "approved", "reviewer": "human"},
        )
    except Exception as exc:  # pragma: no cover -- defensive
        logger.debug("reasoning_digest gate_close emission failed: %s", exc)
    return JSONResponse({"status": "approved", "stage": stage})


# ---------------------------------------------------------------------------
# Gatekeeper endpoints — real-time validation visibility + intervention
# ---------------------------------------------------------------------------

@router.get("/gatekeeper/checks")
async def get_gatekeeper_checks(stage: str | None = None):
    """Get all gatekeeper check results, optionally filtered by stage."""
    from gatekeeper import get_gatekeeper_store
    store = get_gatekeeper_store()
    if stage:
        return JSONResponse({"checks": store.get_checks_for_stage(stage)})
    return JSONResponse({"checks": store.get_all_checks()})


@router.get("/gatekeeper/rejects")
async def get_gatekeeper_rejects():
    """Get all gatekeeper REJECT verdicts — the pipeline-blocking failures."""
    from gatekeeper import get_gatekeeper_store
    return JSONResponse({"rejects": get_gatekeeper_store().get_rejects()})


@router.post("/gatekeeper/halt")
async def halt_gatekeeper(request: Request):
    """User halts the pipeline during a gatekeeper intervention window.

    Body:
        stage: str — the gatekeeper stage to halt (e.g. "production_start")
        comment: str — optional reason for halting
    """
    body = await request.json()
    stage = body.get("stage", "")
    if not stage:
        return JSONResponse({"error": "Missing 'stage' field"}, status_code=400)

    # Write halt signal to approval state file (gatekeeper reads this)
    from callbacks.approval_gate import _read_approval_state, _write_approval_state
    state = _read_approval_state()
    state[f"gatekeeper_{stage}"] = {
        "halted": True,
        "comment": body.get("comment", ""),
        "timestamp": time.time(),
    }
    _write_approval_state(state)

    emit_agui_event("gatekeeper_halted", {
        "stage": stage,
        "comment": body.get("comment", ""),
        "timestamp": time.time(),
    })

    logger.info("Gatekeeper HALTED by user: %s", stage)
    return JSONResponse({"status": "halted", "stage": stage})


@router.post("/regenerate")
async def trigger_regeneration(body: dict):
    """Trigger regeneration at clip / scene / style level.

    Body:
        level: "clip" | "scene" | "style"
        artifact_id: (for clip-level) artifact to regenerate
        scene_num: (for scene-level) scene to regenerate
        comment: (optional) guidance for regeneration
    """
    level = body.get("level", "")
    if level not in ("clip", "scene", "style"):
        return JSONResponse(
            {"error": f"Invalid regeneration level: {level}. Must be clip/scene/style."},
            status_code=400,
        )

    feedback = HumanFeedback(
        id=_store._next_id("regen"),
        feedback_type=FeedbackType.REGENERATE,
        artifact_id=body.get("artifact_id", ""),
        scene_num=body.get("scene_num", 0),
        comment=body.get("comment", ""),
        timestamp=time.time(),
        regeneration_level=level,
    )
    _store.add_feedback(feedback)

    return JSONResponse({
        "status": "queued",
        "regeneration_id": feedback.id,
        "level": level,
    })
