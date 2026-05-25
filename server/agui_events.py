"""
AG-UI events and data models — the events/data layer extracted from agui.py.

This module contains:

1. **Data models** — ArtifactType, ArtifactStatus, ArtifactEvent,
   FeedbackType, HumanFeedback, FeedbackStore.
2. **SSE event bus** — subscribe_agui_events, unsubscribe_agui_events,
   emit_agui_event.
3. **FeedbackStore singleton** — _store, get_feedback_store.
4. **ARCH-H1 slot-state bridge** — _emit_slot_state_from_artifact,
   emit_otio_authoritative.

All real-time events flow through emit_agui_event(), which the unified
CopilotKit SSE stream (in server.py) subscribes to and forwards as
AG-UI CustomEvents alongside agent protocol events.

REST endpoints live in agui.py (and the upcoming agui_approval.py);
this module is pure data + event infrastructure.
"""

from __future__ import annotations

import collections
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_OUTPUT_DIR = "/tmp/documentary-pipeline"


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
