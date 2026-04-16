"""AG-UI compatibility shim for the Strands migration.

Provides the same interfaces that deterministic_steps.py imports:
- ArtifactType, ArtifactStatus, ArtifactEvent
- get_feedback_store() -> FeedbackStore
- emit_agui_event()

These are lightweight replacements that route artifact events through
the DashboardPlugin's PipelineCollector instead of the deleted AG-UI
CopilotKit SSE stream.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Artifact events
# ---------------------------------------------------------------------------

class ArtifactType(str, Enum):
    """Type of artifact produced by the pipeline."""

    VIDEO_CLIP = "video_clip"
    NARRATION = "narration"
    SCENE_SCRIPT = "scene_script"
    VISUAL_CONCEPT = "visual_concept"
    ASSEMBLED_VIDEO = "assembled_video"


class ArtifactStatus(str, Enum):
    """Status of a pipeline artifact."""

    GENERATING = "generating"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    REGENERATING = "regenerating"


@dataclass
class ArtifactEvent:
    """An artifact produced by the pipeline, surfaced to the human."""

    id: str
    artifact_type: ArtifactType
    status: ArtifactStatus
    scene_num: int = 0
    phrase_idx: int = 0
    language: str = ""
    preview_url: str = ""
    duration_sec: float = 0.0
    qa_scores: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
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
# Event emission — routes to PipelineCollector if active
# ---------------------------------------------------------------------------

def emit_agui_event(event_type: str, data: dict[str, Any]) -> None:
    """Emit an artifact/feedback event to the dashboard collector."""
    try:
        from dashboard import get_active_collector

        collector = get_active_collector()
        if collector:
            collector.emit_event(event_type, data)
    except Exception as exc:
        logger.warning("event_type=<%s> | emit_agui_event failed: %s", event_type, exc)
        return
    logger.debug("event_type=<%s> | agui event emitted", event_type)


# ---------------------------------------------------------------------------
# Feedback store
# ---------------------------------------------------------------------------

class FeedbackStore:
    """Thread-safe store for human feedback on pipeline artifacts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._feedback: list[dict[str, Any]] = []
        self._artifacts: dict[str, ArtifactEvent] = {}
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        with self._lock:
            self._counter += 1
            return f"{prefix}-{self._counter:04d}-{int(time.time())}"

    def register_artifact(self, artifact: ArtifactEvent) -> None:
        """Register a new artifact emitted by the pipeline."""
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
        """Get an artifact by ID."""
        with self._lock:
            return self._artifacts.get(artifact_id)

    def get_artifacts_for_scene(self, scene_num: int) -> list[dict[str, Any]]:
        """Get all artifacts for a scene."""
        with self._lock:
            return [
                a.to_dict() for a in self._artifacts.values()
                if a.scene_num == scene_num
            ]

    def get_feedback_for_scene(self, scene_num: int) -> list[dict[str, Any]]:
        """Get all feedback for a scene."""
        with self._lock:
            return [
                fb for fb in self._feedback
                if fb.get("scene_num") == scene_num
            ]

    def get_active_constraints(self) -> dict[str, Any]:
        """Derive active generation constraints from accumulated feedback.

        Scans stored feedback to build negative/positive prompts, style
        adjustments, and a regeneration queue that downstream generators
        use to incorporate human guidance.
        """
        negative_prompts: list[str] = []
        positive_prompts: list[str] = []
        style_adjustments: list[dict[str, Any]] = []
        regeneration_queue: list[dict[str, Any]] = []

        with self._lock:
            for fb in self._feedback:
                fb_type = fb.get("type", "")
                comment = fb.get("comment", "")
                artifact_id = fb.get("artifact_id", "")
                scene_num = fb.get("scene_num", 0)

                if fb_type == "reject" and comment:
                    negative_prompts.append(comment)
                elif fb_type == "approve" and comment:
                    positive_prompts.append(comment)
                elif fb_type == "comment" and comment:
                    # Treat free-text comments as style guidance
                    style_adjustments.append({
                        "scene_num": scene_num,
                        "guidance": comment,
                    })
                elif fb_type == "regenerate":
                    regeneration_queue.append({
                        "artifact_id": artifact_id,
                        "scene_num": scene_num,
                        "level": fb.get("regeneration_level", "clip"),
                    })

        return {
            "negative_prompts": negative_prompts,
            "positive_prompts": positive_prompts,
            "style_adjustments": style_adjustments,
            "regeneration_queue": regeneration_queue,
        }


# Singleton feedback store
_feedback_store: FeedbackStore | None = None
_store_lock = threading.Lock()


def get_feedback_store() -> FeedbackStore:
    """Get or create the singleton feedback store."""
    global _feedback_store
    with _store_lock:
        if _feedback_store is None:
            _feedback_store = FeedbackStore()
        return _feedback_store
