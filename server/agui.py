"""
AG-UI (Agent-User Interaction) — bidirectional feedback between pipeline and human.

This module provides:

1. **Artifact streaming** — as clips/narrations are produced, emit SSE events
   with preview URLs so the human can see production output in real time.

2. **Human feedback** — approve/reject/comment on artifacts.  Feedback is
   stored and flows back into generation prompts for subsequent clips.

3. **Escalation handling** — when the recovery middleware reaches Level 4
   (human escalation), this module surfaces the structured diagnosis and
   proposed actions to the dashboard and waits for human response.

4. **Multi-level regeneration** — human can trigger regeneration at
   clip / scene / style level.

Architecture:
    Pipeline callbacks → emit_agui_event() → unified CopilotKit SSE stream
    Frontend dashboard → POST /agui/* → resolve_escalation() / store feedback
    Pipeline reads feedback via get_feedback_for_scene() / get_active_constraints()

All real-time events flow through the single CopilotKit SSE stream (POST /).
The server.py unified endpoint subscribes to emit_agui_event() and forwards
pipeline events as AG-UI CustomEvents alongside agent protocol events.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agui", tags=["agui"])


# ---------------------------------------------------------------------------
# Artifact events
# ---------------------------------------------------------------------------

class ArtifactType(str, Enum):
    VIDEO_CLIP = "video_clip"
    NARRATION = "narration"
    SCENE_SCRIPT = "scene_script"
    VISUAL_CONCEPT = "visual_concept"
    ASSEMBLED_VIDEO = "assembled_video"


class ArtifactStatus(str, Enum):
    GENERATING = "generating"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    REGENERATING = "regenerating"


@dataclass
class ArtifactEvent:
    """An artifact produced by the pipeline, surfaced to the human."""
    id: str                         # unique artifact ID
    artifact_type: ArtifactType
    status: ArtifactStatus
    scene_num: int = 0
    phrase_idx: int = 0
    language: str = ""
    preview_url: str = ""           # B2 URL or local path
    duration_sec: float = 0.0
    qa_scores: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.artifact_type.value,
            "status": self.status.value,
            "scene_num": self.scene_num,
            "phrase_idx": self.phrase_idx,
            "language": self.language,
            "preview_url": self.preview_url,
            "duration_sec": self.duration_sec,
            "qa_scores": self.qa_scores,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Feedback store
# ---------------------------------------------------------------------------

class FeedbackType(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    COMMENT = "comment"
    REGENERATE = "regenerate"


@dataclass
class HumanFeedback:
    """A piece of human feedback on an artifact or the pipeline."""
    id: str
    feedback_type: FeedbackType
    artifact_id: str = ""           # empty for pipeline-level feedback
    scene_num: int = 0
    comment: str = ""               # free-text comment
    timestamp: float = 0.0
    regeneration_level: str = ""    # "clip" | "scene" | "style" (for regenerate)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.feedback_type.value,
            "artifact_id": self.artifact_id,
            "scene_num": self.scene_num,
            "comment": self.comment,
            "timestamp": self.timestamp,
            "regeneration_level": self.regeneration_level,
        }


class FeedbackStore:
    """Thread-safe store for human feedback.

    The pipeline reads feedback via get_feedback_for_scene() and
    get_active_constraints() to incorporate human guidance into
    subsequent generation steps.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._feedback: list[HumanFeedback] = []
        self._artifacts: dict[str, ArtifactEvent] = {}
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        with self._lock:
            self._counter += 1
            return f"{prefix}-{self._counter:04d}-{int(time.time())}"

    # -- Artifact management -----------------------------------------------

    def register_artifact(self, artifact: ArtifactEvent) -> None:
        """Register a new artifact (emitted by pipeline callbacks)."""
        with self._lock:
            self._artifacts[artifact.id] = artifact
        emit_agui_event("artifact", artifact.to_dict())
        # ARCH-H1: bridge artifact transitions onto the centrepiece timeline.
        # The OTIO dashboard subscribes to "slot_state" to update the three
        # tracks (V1_Video / A1_Narration / A2_Music) without polling.
        _emit_slot_state_from_artifact(artifact)

    def update_artifact_status(self, artifact_id: str, status: ArtifactStatus) -> None:
        """Update an artifact's status."""
        with self._lock:
            art = self._artifacts.get(artifact_id)
            if art:
                art.status = status
        emit_agui_event("artifact_update", {"id": artifact_id, "status": status.value})
        # ARCH-H1: mirror on the slot_state stream the centrepiece subscribes to.
        with self._lock:
            updated = self._artifacts.get(artifact_id)
        if updated is not None:
            _emit_slot_state_from_artifact(updated)

    def get_artifact(self, artifact_id: str) -> Optional[ArtifactEvent]:
        with self._lock:
            return self._artifacts.get(artifact_id)

    def get_artifacts_for_scene(self, scene_num: int) -> list[dict]:
        with self._lock:
            return [
                a.to_dict() for a in self._artifacts.values()
                if a.scene_num == scene_num
            ]

    # -- Feedback management -----------------------------------------------

    def add_feedback(self, feedback: HumanFeedback) -> None:
        """Store human feedback and update artifact status if applicable."""
        with self._lock:
            self._feedback.append(feedback)
            # Update artifact status based on feedback
            if feedback.artifact_id and feedback.artifact_id in self._artifacts:
                art = self._artifacts[feedback.artifact_id]
                if feedback.feedback_type == FeedbackType.APPROVE:
                    art.status = ArtifactStatus.APPROVED
                elif feedback.feedback_type == FeedbackType.REJECT:
                    art.status = ArtifactStatus.REJECTED
                elif feedback.feedback_type == FeedbackType.REGENERATE:
                    art.status = ArtifactStatus.REGENERATING

        emit_agui_event("feedback", feedback.to_dict())
        logger.info(
            "AG-UI feedback: %s on %s (scene %d): %s",
            feedback.feedback_type.value,
            feedback.artifact_id or "pipeline",
            feedback.scene_num,
            feedback.comment if feedback.comment else "(no comment)",
        )

    def get_feedback_for_scene(self, scene_num: int) -> list[dict]:
        """Get all feedback for a scene — used by pipeline to guide generation."""
        with self._lock:
            return [
                fb.to_dict() for fb in self._feedback
                if fb.scene_num == scene_num
            ]

    def get_active_constraints(self) -> dict:
        """Derive active generation constraints from accumulated feedback.

        Returns a dict that pipeline agents can inject into prompts:
        - negative_prompts: things the human doesn't want
        - positive_prompts: things the human liked
        - style_adjustments: adjustments to visual style
        - regeneration_queue: artifacts queued for regeneration
        """
        with self._lock:
            negative: list[str] = []
            positive: list[str] = []
            style_adj: list[str] = []
            regen_queue: list[dict] = []

            for fb in self._feedback:
                if fb.feedback_type == FeedbackType.REJECT and fb.comment:
                    negative.append(fb.comment)
                elif fb.feedback_type == FeedbackType.APPROVE and fb.comment:
                    positive.append(fb.comment)
                elif fb.feedback_type == FeedbackType.COMMENT:
                    # Heuristic: if comment contains "too" or "less" or "more",
                    # treat as style adjustment
                    lower = fb.comment.lower()
                    if any(word in lower for word in ("too", "less", "more", "avoid", "prefer")):
                        style_adj.append(fb.comment)
                    elif any(word in lower for word in ("good", "great", "love", "nice", "perfect")):
                        positive.append(fb.comment)
                    else:
                        negative.append(fb.comment)
                elif fb.feedback_type == FeedbackType.REGENERATE:
                    regen_queue.append({
                        "artifact_id": fb.artifact_id,
                        "scene_num": fb.scene_num,
                        "level": fb.regeneration_level,
                    })

        return {
            "negative_prompts": negative,
            "positive_prompts": positive,
            "style_adjustments": style_adj,
            "regeneration_queue": regen_queue,
        }

    def get_all_feedback(self) -> list[dict]:
        with self._lock:
            return [fb.to_dict() for fb in self._feedback]

    def get_all_artifacts(self) -> list[dict]:
        with self._lock:
            return [a.to_dict() for a in self._artifacts.values()]


# -- Singleton -------------------------------------------------------------

_store = FeedbackStore()


def get_feedback_store() -> FeedbackStore:
    """Return the global FeedbackStore singleton."""
    return _store


# ---------------------------------------------------------------------------
# SSE event bus
# ---------------------------------------------------------------------------

_event_lock = threading.Lock()
_event_subscribers: list[collections.deque] = []  # each subscriber has a deque


def subscribe_agui_events() -> collections.deque:
    """Create a new subscriber queue and return it.

    Uses collections.deque which is thread-safe for append/popleft.
    """
    queue: collections.deque = collections.deque()
    with _event_lock:
        _event_subscribers.append(queue)
    return queue


def unsubscribe_agui_events(queue: collections.deque) -> None:
    """Remove a subscriber queue."""
    with _event_lock:
        try:
            _event_subscribers.remove(queue)
        except ValueError:
            pass


def emit_agui_event(event_type: str, data: dict) -> None:
    """Emit an AG-UI event to all subscribers."""
    event = {"type": event_type, "data": data, "timestamp": time.time()}
    with _event_lock:
        for queue in _event_subscribers:
            queue.append(event)


# ---------------------------------------------------------------------------
# ARCH-H1 slot-state bridge
# ---------------------------------------------------------------------------
#
# The centrepiece OTIO dashboard models each slot as
# ``{track}:{scene_num}:{phrase_idx}``.  Artifacts flowing through the
# existing FeedbackStore carry scene/phrase metadata; we translate every
# ``ArtifactEvent`` into a ``slot_state`` SSE event so the frontend never
# has to poll to learn that a slot changed state.
_ARTIFACT_TYPE_TO_TRACK = {
    "video_clip": "V1_Video",
    "narration": "A1_Narration",
    "music": "A2_Music",
}

_STATUS_TO_SLOT_STATE = {
    "generating": "in_progress",
    "regenerating": "in_progress",
    "pending_review": "delivered",
    "approved": "delivered",
    "rejected": "failed",
}


def _emit_slot_state_from_artifact(artifact: "ArtifactEvent") -> None:
    track = _ARTIFACT_TYPE_TO_TRACK.get(artifact.artifact_type.value)
    if track is None:
        return
    slot_state = _STATUS_TO_SLOT_STATE.get(artifact.status.value, "pending")
    slot_id = f"{track.split('_')[0]}:{artifact.scene_num}:{artifact.phrase_idx}"
    emit_agui_event("slot_state", {
        "slot_id": slot_id,
        "track": track,
        "scene_num": artifact.scene_num,
        "phrase_idx": artifact.phrase_idx,
        "status": slot_state,
        "artifact_id": artifact.id,
        "artifact_status": artifact.status.value,
        "preview_url": artifact.preview_url,
        "duration_sec": artifact.duration_sec,
        "qa_scores": artifact.qa_scores,
    })


def emit_otio_authoritative(timeline_path: str = "", reason: str = "") -> None:
    """Emit the ``otio_authoritative`` flip event (ARCH-H2).

    Called from :func:`server.callbacks.otio_state.set_otio_state` when the
    timeline crystallises.  Event-driven by design — the UI uses this to
    drop the reconciliation overlay and lock the timeline to scale.
    """
    emit_agui_event("otio_authoritative", {
        "timeline_path": timeline_path,
        "reason": reason,
    })


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@router.get("/reasoning/digests")
async def get_reasoning_digests(
    limit: int = 50,
    since: float | None = None,
    phase: str | None = None,
    importance: str | None = None,
):
    """Get reasoning digests — concise summaries of agent activity.

    These are batch-processed from raw traces by the DigestEngine background
    thread.  Each digest summarises a burst of agent activity into a 1-2
    sentence summary with structured details (ratings, errors, token costs,
    production planning decisions).

    Query params:
        limit:      max digests (default 50)
        since:      only digests after this Unix timestamp (for polling)
        phase:      filter by pipeline phase (scenario, audio, visual_direction, production, assembly)
        importance: filter by importance (low, medium, high)
    """
    try:
        from plugins.reasoning_digest import get_digest_engine

        engine = get_digest_engine()

        if since and since > 0:
            digests = engine.get_since(since, limit=limit)
        else:
            digests = engine.get_recent(limit)

        # Apply filters
        if phase:
            digests = [d for d in digests if d.get("phase") == phase]
        if importance:
            if importance == "medium":
                # "Medium+" means medium AND high
                digests = [d for d in digests if d.get("importance") in ("medium", "high")]
            else:
                digests = [d for d in digests if d.get("importance") == importance]

        return JSONResponse({"digests": digests, "count": len(digests)})

    except Exception as e:
        return JSONResponse(
            {"digests": [], "count": 0, "error": str(e)},
            status_code=200,
        )


@router.get("/reasoning/raw")
async def get_reasoning_traces_raw(
    agent: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
    since: float | None = None,
):
    """Get raw reasoning traces (for drill-down from a digest).

    The frontend should prefer ``/reasoning/digests`` for the main view.
    Use this endpoint when the user expands a digest and wants to see the
    underlying raw events.

    Query params:
        agent:      filter by agent name
        event_type: filter by event type (llm_request, llm_response, etc.)
        limit:      max rows (default 50)
        since:      only rows after this Unix timestamp (for polling)
    """
    try:
        from plugins.reasoning_trace import _REASONING_DB
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(_REASONING_DB, timeout=5)
        conn.row_factory = _sqlite3.Row

        query = "SELECT * FROM reasoning_log WHERE 1=1"
        params: list = []

        if agent:
            query += " AND agent_name = ?"
            params.append(agent)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        if since:
            query += " AND timestamp > ?"
            params.append(since)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()

        traces = []
        for row in rows:
            traces.append({
                "id": row["id"],
                "timestamp": row["timestamp"],
                "event_type": row["event_type"],
                "agent_name": row["agent_name"],
                "model": row["model"],
                "content": row["content"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            })

        # Return in chronological order (query was DESC for LIMIT)
        traces.reverse()
        return JSONResponse({"traces": traces, "count": len(traces)})

    except Exception as e:
        return JSONResponse(
            {"traces": [], "count": 0, "error": str(e)},
            status_code=200,
        )


@router.get("/artifacts")
async def get_artifacts(type: str | None = None):
    """Get all artifacts produced by the pipeline.

    Optional query param ?type= filters by artifact type.
    """
    arts = _store.get_all_artifacts()
    if type:
        arts = [a for a in arts if a.get("type") == type]
    return JSONResponse({"artifacts": arts})


@router.get("/artifacts/scene/{scene_num}")
async def get_scene_artifacts(scene_num: int):
    """Get all artifacts for a specific scene."""
    return JSONResponse({"artifacts": _store.get_artifacts_for_scene(scene_num)})


@router.post("/artifacts/ingest")
async def ingest_artifact(request: Request):
    """Register an artifact from an external pipeline runner.

    This is the AG-UI counterpart of /dashboard/ingest — it bridges
    run_pipeline.py (separate process) with the AG-UI artifact store
    so the frontend Artifacts, Clip Reviewer, Scenario Editor, etc.
    tabs all populate canonically.

    Body:
        id:           str  — unique artifact ID
        type:         str  — "video_clip", "narration", "scene_script",
                             "visual_concept", "assembled_video"
        status:       str  — "generating", "pending_review", "approved", etc.
        scene_num:    int  — scene number
        phrase_idx:   int  — phrase index within scene
        language:     str  — language code
        preview_url:  str  — URL or path to the artifact
        duration_sec: float — duration in seconds
        qa_scores:    dict  — QA quality scores
        metadata:     dict  — additional metadata (prompt, lora, etc.)
    """

    body = await request.json()

    art_id = body.get("id", _store._next_id("art"))
    try:
        art_type = ArtifactType(body.get("type", "video_clip"))
    except ValueError:
        art_type = ArtifactType.VIDEO_CLIP

    try:
        art_status = ArtifactStatus(body.get("status", "pending_review"))
    except ValueError:
        art_status = ArtifactStatus.PENDING_REVIEW

    artifact = ArtifactEvent(
        id=art_id,
        artifact_type=art_type,
        status=art_status,
        scene_num=body.get("scene_num", 0),
        phrase_idx=body.get("phrase_idx", 0),
        language=body.get("language", ""),
        preview_url=body.get("preview_url", ""),
        duration_sec=body.get("duration_sec", 0.0),
        qa_scores=body.get("qa_scores", {}),
        metadata=body.get("metadata", {}),
        timestamp=time.time(),
    )
    _store.register_artifact(artifact)
    logger.info(
        "AG-UI ingest: %s %s (scene %d, phrase %d)",
        art_type.value, art_id, artifact.scene_num, artifact.phrase_idx,
    )
    return JSONResponse({"status": "ok", "artifact_id": art_id})


@router.post("/feedback")
async def post_feedback(body: dict):
    """Submit human feedback on an artifact or the pipeline.

    Body:
        feedback_type: "approve" | "reject" | "comment" | "regenerate"
        artifact_id: (optional) artifact to provide feedback on
        scene_num: (optional) scene number
        comment: (optional) free-text comment
        regeneration_level: (optional) "clip" | "scene" | "style"
    """
    try:
        fb_type = FeedbackType(body.get("feedback_type", "comment"))
    except ValueError:
        return JSONResponse(
            {"error": f"Invalid feedback_type: {body.get('feedback_type')}"},
            status_code=400,
        )

    feedback = HumanFeedback(
        id=_store._next_id("fb"),
        feedback_type=fb_type,
        artifact_id=body.get("artifact_id", ""),
        scene_num=body.get("scene_num", 0),
        comment=body.get("comment", ""),
        timestamp=time.time(),
        regeneration_level=body.get("regeneration_level", ""),
    )
    _store.add_feedback(feedback)
    return JSONResponse({"status": "ok", "feedback_id": feedback.id})


@router.get("/feedback")
async def get_feedback():
    """Get all accumulated human feedback."""
    return JSONResponse({"feedback": _store.get_all_feedback()})


@router.get("/constraints")
async def get_constraints():
    """Get active generation constraints derived from human feedback.

    Pipeline agents can poll this to incorporate human guidance.
    """
    return JSONResponse(_store.get_active_constraints())


@router.get("/escalations")
async def get_escalations():
    """Get all escalation requests (pending and resolved)."""
    from recovery import get_all_escalations
    return JSONResponse({"escalations": get_all_escalations()})


@router.get("/escalations/pending")
async def get_pending_escalations_endpoint():
    """Get pending escalation requests that need human response."""
    from recovery import get_pending_escalations
    return JSONResponse({"escalations": get_pending_escalations()})


@router.post("/escalations/{escalation_id}/respond")
async def respond_to_escalation(escalation_id: str, body: dict):
    """Respond to an escalation request.

    Body:
        action: "retry_with_fix" | "skip" | "abort" | "amend"
        kwargs: (optional) amended kwargs for "amend" action
        comment: (optional) human comment
    """
    from recovery import resolve_escalation

    action = body.get("action", "")
    if not action:
        return JSONResponse(
            {"error": "Missing 'action' field"},
            status_code=400,
        )

    response = {
        "action": action,
        "kwargs": body.get("kwargs", {}),
        "comment": body.get("comment", ""),
        "timestamp": time.time(),
    }

    success = resolve_escalation(escalation_id, response)
    if not success:
        return JSONResponse(
            {"error": f"Escalation {escalation_id} not found"},
            status_code=404,
        )

    return JSONResponse({"status": "resolved", "escalation_id": escalation_id})


# ---------------------------------------------------------------------------
# File-backed AG-UI endpoints — read pipeline output from disk
# ---------------------------------------------------------------------------

_OUTPUT_DIR = os.environ.get("PIPELINE_OUTPUT_DIR", "/tmp/documentary-pipeline")

# Approval gate: sequential workflow state
# Stages: scenario -> prompts -> clips -> timeline -> assembly
# Shared module used by both the backend (this file) and the pipeline callbacks.
from callbacks.approval_gate import (
    _read_approval_state,
    _write_approval_state,
    is_stage_approved as _is_stage_approved,
)

_STAGE_ORDER = ["scenario", "prompts", "clips", "timeline", "assembly"]


@router.get("/approval-state")
async def get_approval_state():
    """Return current approval state for all stages."""
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
    state = _read_approval_state()
    state[stage] = {"approved": True, "timestamp": time.time()}
    _write_approval_state(state)
    logger.info("Stage '%s' approved", stage)
    return JSONResponse({"status": "approved", "stage": stage})


@router.get("/scenes")
async def get_scenes():
    """Return scene list from the pipeline's scenes backup file.

    The scenario director writes _scenes_backup.json in the timelines dir.
    """
    scenes_path = os.path.join(_OUTPUT_DIR, "timelines", "_scenes_backup.json")
    if not os.path.exists(scenes_path):
        return JSONResponse({"scenes": []})
    try:
        with open(scenes_path) as f:
            scenes = json.load(f)
        return JSONResponse({"scenes": scenes})
    except Exception as exc:
        logger.warning("Failed to read scenes: %s", exc)
        return JSONResponse({"scenes": []})


@router.post("/backfill-prompts")
async def backfill_prompts():
    """Backfill old status files that lack prompt_full.

    Reads the full prompt from the visual style backup concepts list
    and patches each status JSON on disk so that prompt_full is populated.
    Returns the count of files patched.
    """
    import glob as _glob
    import re as _re

    # Try to load the visual style backup which may contain the full prompts
    style_path = os.path.join(_OUTPUT_DIR, "timelines", "_visual_style_backup.json")
    style_data: dict = {}
    if os.path.exists(style_path):
        try:
            with open(style_path) as f:
                style_data = json.load(f)
        except Exception:
            pass

    # Build lookup of full prompts from concepts in style backup
    concept_prompts: dict[tuple[int, int], str] = {}
    for concept in style_data.get("concepts", []):
        key = (concept.get("scene_num", 0), concept.get("phrase_idx", 0))
        prompt = concept.get("prompt", "")
        if prompt and len(prompt) > 200:
            concept_prompts[key] = prompt

    patched = 0
    pattern = os.path.join(_OUTPUT_DIR, "video", "*_status.json")
    for path in sorted(_glob.glob(pattern)):
        fname = os.path.basename(path)
        m = _re.match(r"scene_(\d+)_phrase_(\d+)_status\.json", fname)
        if not m:
            continue
        scene_num = int(m.group(1))
        phrase_idx = int(m.group(2))
        try:
            with open(path) as f:
                data = json.load(f)
            existing_full = data.get("prompt_full", "")
            preview = data.get("prompt_preview", "")
            # Only patch if prompt_full is missing or same length as truncated preview
            if not existing_full or (preview and len(existing_full) <= len(preview)):
                full_prompt = concept_prompts.get((scene_num, phrase_idx), "")
                if full_prompt:
                    data["prompt_full"] = full_prompt
                    with open(path, "w") as f:
                        json.dump(data, f, indent=2)
                    patched += 1
        except Exception as exc:
            logger.debug("Failed to backfill %s: %s", path, exc)

    return JSONResponse({"status": "ok", "patched": patched, "total_concepts": len(concept_prompts)})


@router.get("/visual-concepts")
async def get_visual_concepts():
    """Return visual concepts derived from video status files.

    Gated: requires 'scenario' stage to be approved first.
    """
    if not _is_stage_approved("scenario"):
        return JSONResponse({
            "concepts": [],
            "visual_style": {},
            "gate": {"blocked": True, "requires": "scenario", "message": "Approve the scenario first to unlock visual prompts"},
        })
    import glob as _glob
    import re as _re

    # Load visual style backup for global info and concept-level prompt reasoning
    style_path = os.path.join(_OUTPUT_DIR, "timelines", "_visual_style_backup.json")
    style: dict = {}
    concept_reasoning: dict[tuple[int, int], str] = {}
    concept_full_prompts: dict[tuple[int, int], str] = {}
    if os.path.exists(style_path):
        try:
            with open(style_path) as f:
                style = json.load(f)
            # Extract per-concept reasoning and full prompts from backup
            for concept in style.get("concepts", []):
                key = (concept.get("scene_num", 0), concept.get("phrase_idx", 0))
                reasoning = concept.get("prompt_reasoning", concept.get("reasoning", ""))
                if reasoning:
                    concept_reasoning[key] = reasoning
                full_prompt = concept.get("prompt", "")
                if full_prompt:
                    concept_full_prompts[key] = full_prompt
        except Exception:
            pass

    concepts: list[dict] = []
    pattern = os.path.join(_OUTPUT_DIR, "video", "*_status.json")
    for path in sorted(_glob.glob(pattern)):
        fname = os.path.basename(path)
        # Extract scene/phrase from filename: scene_001_phrase_002_status.json
        m = _re.match(r"scene_(\d+)_phrase_(\d+)_status\.json", fname)
        if not m:
            continue
        scene_num = int(m.group(1))
        phrase_idx = int(m.group(2))
        try:
            with open(path) as f:
                data = json.load(f)
            key = (scene_num, phrase_idx)
            # Use prompt_full from status file, falling back to style backup, then preview
            prompt = data.get("prompt_full", "")
            if not prompt or len(prompt) <= 200:
                prompt = concept_full_prompts.get(key, data.get("prompt_preview", ""))
            concepts.append({
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
                "prompt": prompt,
                "prompt_reasoning": concept_reasoning.get(key, data.get("prompt_reasoning", "")),
                "quality": data.get("quality", "unknown"),
                "qa_reason": data.get("qa_reason", ""),
                "attempts": data.get("attempts", 0),
                "status": data.get("status", "unknown"),
                "lora_id": data.get("lora_id", ""),
                "lora_weight": data.get("lora_weight", 0.0),
                "camera_style": data.get("camera_style", ""),
                "mood": data.get("mood", ""),
                "start_time": 0.0,
                "end_time": 0.0,
                "duration": 0.0,
                "environment": "",
            })
        except Exception as exc:
            logger.debug("Failed to read %s: %s", path, exc)

    return JSONResponse({"concepts": concepts, "visual_style": style})


@router.get("/clips")
async def get_clips():
    """Return video clips with QA status for the Clip Reviewer tab.

    Gated: requires 'prompts' stage to be approved first.
    """
    if not _is_stage_approved("prompts"):
        return JSONResponse({
            "clips": [],
            "gate": {"blocked": True, "requires": "prompts", "message": "Approve visual prompts first to unlock clip review"},
        })
    import glob as _glob
    import re as _re

    clips: list[dict] = []
    pattern = os.path.join(_OUTPUT_DIR, "video", "*_status.json")
    for path in sorted(_glob.glob(pattern)):
        fname = os.path.basename(path)
        m = _re.match(r"scene_(\d+)_phrase_(\d+)_status\.json", fname)
        if not m:
            continue
        scene_num = int(m.group(1))
        phrase_idx = int(m.group(2))
        try:
            with open(path) as f:
                data = json.load(f)
            # Check if the actual video file exists
            video_name = fname.replace("_status.json", ".mp4")
            video_path = os.path.join(_OUTPUT_DIR, "video", video_name)
            clips.append({
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
                "video_path": video_path if os.path.exists(video_path) else "",
                "narration_text": data.get("prompt_full", data.get("prompt_preview", "")),
                "duration": data.get("duration", 0.0),
                "lora_id": data.get("lora_id", ""),
                "status": "approved" if data.get("quality") == "acceptable" else "pending",
                "quality": data.get("quality", "unknown"),
                "qa_reason": data.get("qa_reason", ""),
                "attempts": data.get("attempts", 0),
            })
        except Exception as exc:
            logger.debug("Failed to read %s: %s", path, exc)

    return JSONResponse({"clips": clips})


@router.get("/timeline")
async def get_timeline():
    """Read the OTIO timeline file and return structured track/clip data.

    Gated: requires 'clips' stage to be approved first.
    """
    if not _is_stage_approved("clips"):
        return JSONResponse({
            "timeline": None,
            "gate": {"blocked": True, "requires": "clips", "message": "Approve clips first to unlock the timeline"},
        })
    import glob as _glob

    # Find the most recent OTIO file (skip backups starting with _)
    pattern = os.path.join(_OUTPUT_DIR, "timelines", "*.otio")
    otio_files = [
        f for f in sorted(_glob.glob(pattern))
        if not os.path.basename(f).startswith("_")
    ]
    if not otio_files:
        return JSONResponse({"timeline": None})

    otio_path = otio_files[-1]  # most recent
    try:
        with open(otio_path) as f:
            otio_data = json.load(f)

        tracks: list[dict] = []
        for track in otio_data.get("tracks", {}).get("children", []):
            clips: list[dict] = []
            gaps: list[dict] = []
            for child in track.get("children", []):
                schema = child.get("OTIO_SCHEMA", "")
                sr = child.get("source_range", {})
                duration_val = sr.get("duration", {}).get("value", 0)
                duration_rate = sr.get("duration", {}).get("rate", 24)
                duration_sec = duration_val / duration_rate if duration_rate else 0

                if "Gap" in schema:
                    gaps.append({
                        "name": child.get("name", "gap"),
                        "metadata": child.get("metadata", {}),
                    })
                else:
                    clips.append({
                        "name": child.get("name", ""),
                        "duration": round(duration_sec, 2),
                        "metadata": child.get("metadata", {}),
                    })

            tracks.append({
                "name": track.get("name", ""),
                "kind": track.get("kind", ""),
                "clips": clips,
                "gaps": gaps,
                "total_clips": len(clips),
                "total_gaps": len(gaps),
            })

        return JSONResponse({
            "timeline": {
                "timeline_name": otio_data.get("name", os.path.basename(otio_path)),
                "tracks": tracks,
            }
        })
    except Exception as exc:
        logger.warning("Failed to read OTIO timeline: %s", exc)
        return JSONResponse({"timeline": None})


@router.get("/qa-results")
async def get_qa_results():
    """Return QA results by checking pipeline output completeness.

    Derives pass/fail per phase by checking whether expected output
    files exist and have valid content.  Includes per-clip detail.
    """
    results: list[dict] = []

    # Scenario: check if scenes backup exists with content
    scenes_path = os.path.join(_OUTPUT_DIR, "timelines", "_scenes_backup.json")
    if os.path.exists(scenes_path):
        try:
            with open(scenes_path) as f:
                scenes = json.load(f)
            if scenes and len(scenes) > 0:
                scene_details = []
                for s in scenes:
                    scene_details.append({
                        "scene_num": s.get("scene_num", 0),
                        "title": s.get("title", ""),
                        "duration_sec": s.get("duration_sec", 0),
                        "voices": len(s.get("voices", [])),
                        "has_hook": bool(s.get("dopamine_hook")),
                    })
                results.append({
                    "phase": "scenario",
                    "valid": True,
                    "message": f"{len(scenes)} scenes generated with V1/V2/V3 voices",
                    "details": scene_details,
                })
            else:
                results.append({
                    "phase": "scenario",
                    "valid": False,
                    "errors": "Scenes file is empty",
                })
        except Exception as exc:
            results.append({
                "phase": "scenario",
                "valid": False,
                "errors": str(exc),
            })

    # Audio: check WAV files exist
    import glob as _glob
    wavs = sorted(_glob.glob(os.path.join(_OUTPUT_DIR, "audio", "*.wav")))
    if wavs:
        audio_details = [{"file": os.path.basename(w), "size_kb": round(os.path.getsize(w) / 1024, 1)} for w in wavs]
        results.append({
            "phase": "audio",
            "valid": True,
            "message": f"{len(wavs)} narration WAV files produced",
            "details": audio_details,
        })
    elif os.path.exists(os.path.join(_OUTPUT_DIR, "audio")):
        results.append({
            "phase": "audio",
            "valid": False,
            "errors": "Audio directory exists but no WAV files found",
        })

    # Visual Direction: check visual style backup exists
    style_path = os.path.join(_OUTPUT_DIR, "timelines", "_visual_style_backup.json")
    if os.path.exists(style_path):
        try:
            with open(style_path) as f:
                vs = json.load(f)
            concept_count = len(vs.get("concepts", []))
            results.append({
                "phase": "visual_direction",
                "valid": True,
                "message": f"Visual style generated with {concept_count} concepts",
                "details": [{"style": vs.get("style", ""), "palette": vs.get("palette", ""), "concepts": concept_count}],
            })
        except Exception:
            results.append({
                "phase": "visual_direction",
                "valid": True,
                "message": "Visual style and concepts generated",
            })

    # Production: check video files with per-clip QA detail
    import re as _re
    videos = sorted(_glob.glob(os.path.join(_OUTPUT_DIR, "video", "*.mp4")))
    status_files = sorted(_glob.glob(os.path.join(_OUTPUT_DIR, "video", "*_status.json")))
    if videos or status_files:
        clip_details = []
        for sf in status_files:
            fname = os.path.basename(sf)
            m = _re.match(r"scene_(\d+)_phrase_(\d+)_status\.json", fname)
            if not m:
                continue
            try:
                with open(sf) as f:
                    sd = json.load(f)
                clip_details.append({
                    "scene_num": int(m.group(1)),
                    "phrase_idx": int(m.group(2)),
                    "quality": sd.get("quality", "unknown"),
                    "qa_reason": sd.get("qa_reason", ""),
                    "attempts": sd.get("attempts", 0),
                    "has_video": os.path.exists(sf.replace("_status.json", ".mp4")),
                })
            except Exception:
                pass
        passed = sum(1 for c in clip_details if c["quality"] in ("acceptable", "excellent", "good"))
        failed = sum(1 for c in clip_details if c["quality"] not in ("acceptable", "excellent", "good", "unknown"))
        results.append({
            "phase": "production",
            "valid": len(videos) > 0,
            "message": f"{len(videos)} video clips produced ({passed} passed QA, {failed} failed)",
            "errors": f"QA status files exist ({len(status_files)}) but no MP4 files" if not videos else "",
            "details": clip_details,
        })

    # Assembly: check for final documentary
    assembly_dir = os.path.join(_OUTPUT_DIR, "assembly")
    final_vids = _glob.glob(os.path.join(assembly_dir, "*.mp4")) if os.path.exists(assembly_dir) else []
    if final_vids:
        results.append({
            "phase": "assembly",
            "valid": True,
            "message": f"Final documentary assembled: {os.path.basename(final_vids[0])}",
            "details": [{"file": os.path.basename(v), "size_mb": round(os.path.getsize(v) / (1024*1024), 1)} for v in final_vids],
        })

    return JSONResponse({"results": results})


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


# ---------------------------------------------------------------------------
# ARCH-H1 / ARCH-H2 / ARCH-H3 — OTIO centrepiece timeline endpoints
# ---------------------------------------------------------------------------
#
# These endpoints back the new dashboard centrepiece: an authoritative
# (or draft-with-reconciliation-overlay) OTIO timeline rendered on three
# canonical tracks.  All endpoints are pure read models — no mutation —
# and a dedicated SSE stream delivers slot-state transitions so the UI
# never polls.


@router.get("/otio/state")
async def get_otio_state_view():
    """Return the centrepiece OTIO view.

    Response shape::

        {
          "state": "draft"|"authoritative",
          "total_duration_sec": float,
          "tracks": [
            {"name": "V1_Video", "kind": "video", "slots": [...], "total_slots": N},
            {"name": "A1_Narration", "kind": "audio", "slots": [...], ...},
            {"name": "A2_Music", "kind": "audio", "slots": [...], ...}
          ],
          "reconciliation": [...],  // empty when state=="authoritative"
          "source_file": "/tmp/documentary-pipeline/timelines/<file>.otio"
        }
    """
    from otio_timeline_model import build_timeline_view

    artifacts = [a.to_dict() for a in _store.get_all_artifacts()]
    view = build_timeline_view(_OUTPUT_DIR, feedback_artifacts=artifacts)
    return JSONResponse(view.to_dict())


@router.get("/slots/{slot_id}/detail")
async def get_slot_detail(slot_id: str):
    """Aggregate artifact history, QA, reasoning, ledger, rung, preview.

    Pure read-only; never mutates state.  See
    :mod:`server.slot_detail_model` for the underlying builder.
    """
    from otio_timeline_model import parse_slot_id
    from slot_detail_model import build_slot_detail

    try:
        parse_slot_id(slot_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    artifacts = [a.to_dict() for a in _store.get_all_artifacts()]
    detail = build_slot_detail(
        slot_id,
        _OUTPUT_DIR,
        feedback_artifacts=artifacts,
        state=None,
    )
    return JSONResponse(detail.to_dict())


@router.get("/slots/{slot_id}/thumbnail")
async def get_slot_thumbnail(slot_id: str):
    """Return the first-frame thumbnail for a delivered video slot.

    Best-effort.  If ``ffmpeg`` is on PATH and a delivered MP4 exists for
    the slot we extract a single frame on-demand (cached on disk next to
    the MP4).  Otherwise we return 404.
    """
    from fastapi.responses import FileResponse
    from otio_timeline_model import (
        TRACK_V1_VIDEO,
        parse_slot_id,
    )

    try:
        track, scene, phrase = parse_slot_id(slot_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if track != TRACK_V1_VIDEO:
        return JSONResponse({"error": "not a video slot"}, status_code=400)

    mp4_path = os.path.join(
        _OUTPUT_DIR,
        "video",
        f"scene_{scene:03d}_phrase_{phrase:03d}.mp4",
    )
    if not os.path.exists(mp4_path):
        return JSONResponse({"error": "no delivered clip"}, status_code=404)
    thumb_path = mp4_path.replace(".mp4", "_thumb.jpg")
    if not os.path.exists(thumb_path):
        try:
            import subprocess
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", "0.1", "-i", mp4_path,
                    "-frames:v", "1", "-q:v", "4", thumb_path,
                ],
                check=True,
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("thumbnail extraction failed for %s: %s", mp4_path, exc)
            return JSONResponse({"error": "thumbnail unavailable"}, status_code=404)
    return FileResponse(thumb_path, media_type="image/jpeg")


@router.get("/slots/{slot_id}/waveform")
async def get_slot_waveform(slot_id: str, samples: int = 240):
    """Return a downsampled RMS envelope for the WAV backing this slot."""
    from otio_timeline_model import (
        TRACK_A1_NARRATION,
        TRACK_A2_MUSIC,
        parse_slot_id,
    )

    try:
        track, scene, phrase = parse_slot_id(slot_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if track == TRACK_A1_NARRATION:
        wav_path = os.path.join(
            _OUTPUT_DIR, "audio", f"scene_{scene:03d}_phrase_{phrase:03d}.wav"
        )
    elif track == TRACK_A2_MUSIC:
        wav_path = os.path.join(
            _OUTPUT_DIR, "music", f"scene_{scene:03d}_phrase_{phrase:03d}.wav"
        )
    else:
        return JSONResponse({"error": "not an audio slot"}, status_code=400)

    if not os.path.exists(wav_path):
        return JSONResponse({"error": "no delivered audio"}, status_code=404)
    samples = max(16, min(samples, 2000))

    try:
        import wave
        with wave.open(wav_path, "rb") as wf:
            n_frames = wf.getnframes()
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            raw = wf.readframes(n_frames)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"wav read failed: {exc}"}, status_code=500)

    # Cheap RMS downsample without numpy dependency (ARCH-H1 doesn't need it).
    import struct
    if sampwidth == 2:
        fmt = "<" + "h" * (len(raw) // 2)
    elif sampwidth == 4:
        fmt = "<" + "i" * (len(raw) // 4)
    else:
        return JSONResponse({"error": "unsupported sample width"}, status_code=415)
    try:
        all_samples = struct.unpack(fmt, raw)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"decode failed: {exc}"}, status_code=500)
    # Fold channels down to mono by averaging interleaved samples.
    if n_channels > 1:
        mono = [
            sum(all_samples[i : i + n_channels]) / n_channels
            for i in range(0, len(all_samples), n_channels)
        ]
    else:
        mono = list(all_samples)

    if not mono:
        return JSONResponse({"samples": [], "duration_sec": 0})

    bucket = max(1, len(mono) // samples)
    peak = float(1 << (8 * sampwidth - 1))
    envelope = []
    for i in range(0, len(mono), bucket):
        chunk = mono[i : i + bucket]
        if not chunk:
            continue
        mx = max(abs(v) for v in chunk)
        envelope.append(round(mx / peak, 4))
    return JSONResponse({
        "samples": envelope[:samples],
        "duration_sec": n_frames / framerate if framerate else 0,
    })


@router.get("/stream")
async def stream_events(request: Request):
    """Server-Sent Events stream for the centrepiece dashboard.

    Subscribes to the shared AG-UI event bus and relays every event as
    SSE.  The dashboard listens for ``slot_state``, ``otio_authoritative``,
    and ``artifact_update`` to drive the three-track view without ever
    polling.
    """

    async def _event_gen():
        queue = subscribe_agui_events()
        try:
            # Kick the connection with an initial snapshot event so late
            # subscribers see the current OTIO state without re-fetching.
            from otio_timeline_model import build_timeline_view
            artifacts = [a.to_dict() for a in _store.get_all_artifacts()]
            view = build_timeline_view(_OUTPUT_DIR, feedback_artifacts=artifacts)
            snapshot = {
                "type": "otio_snapshot",
                "data": view.to_dict(),
                "timestamp": time.time(),
            }
            yield f"event: {snapshot['type']}\ndata: {json.dumps(snapshot['data'])}\n\n"

            last_heartbeat = time.time()
            while True:
                if await request.is_disconnected():
                    break
                if queue:
                    event = queue.popleft()
                    ev_type = event.get("type", "message")
                    payload = json.dumps({
                        "data": event.get("data"),
                        "timestamp": event.get("timestamp"),
                    })
                    yield f"event: {ev_type}\ndata: {payload}\n\n"
                    last_heartbeat = time.time()
                else:
                    if time.time() - last_heartbeat > 15:
                        yield ": heartbeat\n\n"
                        last_heartbeat = time.time()
                    await asyncio.sleep(0.15)
        finally:
            unsubscribe_agui_events(queue)

    return StreamingResponse(_event_gen(), media_type="text/event-stream")
