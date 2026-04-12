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
    Pipeline callbacks → emit_agui_event() → SSE stream → Frontend dashboard
    Frontend dashboard → POST /agui/* → resolve_escalation() / store feedback
    Pipeline reads feedback via get_feedback_for_scene() / get_active_constraints()
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

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

    def update_artifact_status(self, artifact_id: str, status: ArtifactStatus) -> None:
        """Update an artifact's status."""
        with self._lock:
            art = self._artifacts.get(artifact_id)
            if art:
                art.status = status
        emit_agui_event("artifact_update", {"id": artifact_id, "status": status.value})

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
            feedback.comment[:100] if feedback.comment else "(no comment)",
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
_event_subscribers: list[list[dict]] = []  # each subscriber has a queue


def subscribe_agui_events() -> list[dict]:
    """Create a new subscriber queue and return it.

    The SSE endpoint appends to this list and the generator reads from it.
    """
    queue: list[dict] = []
    with _event_lock:
        _event_subscribers.append(queue)
    return queue


def unsubscribe_agui_events(queue: list[dict]) -> None:
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
# REST endpoints
# ---------------------------------------------------------------------------

@router.get("/stream")
async def agui_stream():
    """SSE endpoint for AG-UI events (artifacts, escalations, feedback)."""
    import asyncio
    from starlette.responses import StreamingResponse

    queue = subscribe_agui_events()

    async def event_generator():
        try:
            while True:
                if queue:
                    event = queue.pop(0)
                    yield f"data: {json.dumps(event)}\n\n"
                else:
                    # Heartbeat
                    yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': time.time()})}\n\n"
                await asyncio.sleep(1.0)
        finally:
            unsubscribe_agui_events(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/artifacts")
async def get_artifacts():
    """Get all artifacts produced by the pipeline."""
    return JSONResponse({"artifacts": _store.get_all_artifacts()})


@router.get("/artifacts/scene/{scene_num}")
async def get_scene_artifacts(scene_num: int):
    """Get all artifacts for a specific scene."""
    return JSONResponse({"artifacts": _store.get_artifacts_for_scene(scene_num)})


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
