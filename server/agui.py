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

import collections
import json
import logging
import os
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
                    event = queue.popleft()
                    yield f"data: {json.dumps(event)}\n\n"
                    await asyncio.sleep(0.05)  # small yield for burst draining
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
    from fastapi import Request as _Req  # already imported at module level

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

_OUTPUT_DIR = os.environ.get("PIPELINE_OUTPUT_DIR", "/workspace/documentary-output")


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


@router.get("/visual-concepts")
async def get_visual_concepts():
    """Return visual concepts derived from video status files.

    Each video status JSON has a prompt_preview and scene/phrase info
    embedded in its filename (scene_NNN_phrase_NNN_status.json).
    Also reads _visual_style_backup.json for global style info.
    """
    import glob as _glob
    import re as _re

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
            concepts.append({
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
                "prompt": data.get("prompt_preview", ""),
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

    # Also read visual style backup for global info
    style_path = os.path.join(_OUTPUT_DIR, "timelines", "_visual_style_backup.json")
    style = {}
    if os.path.exists(style_path):
        try:
            with open(style_path) as f:
                style = json.load(f)
        except Exception:
            pass

    return JSONResponse({"concepts": concepts, "visual_style": style})


@router.get("/clips")
async def get_clips():
    """Return video clips with QA status for the Clip Reviewer tab."""
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
                "narration_text": data.get("prompt_preview", "")[:200],
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
    """Read the OTIO timeline file and return structured track/clip data."""
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
    files exist and have valid content.
    """
    results: list[dict] = []

    # Scenario: check if scenes backup exists with content
    scenes_path = os.path.join(_OUTPUT_DIR, "timelines", "_scenes_backup.json")
    if os.path.exists(scenes_path):
        try:
            with open(scenes_path) as f:
                scenes = json.load(f)
            if scenes and len(scenes) > 0:
                results.append({
                    "phase": "scenario",
                    "valid": True,
                    "message": f"{len(scenes)} scenes generated with V1/V2/V3 voices",
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
    wavs = _glob.glob(os.path.join(_OUTPUT_DIR, "audio", "*.wav"))
    if wavs:
        results.append({
            "phase": "audio",
            "valid": True,
            "message": f"{len(wavs)} narration WAV files produced",
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
        results.append({
            "phase": "visual_direction",
            "valid": True,
            "message": "Visual style and concepts generated",
        })

    # Production: check video files
    videos = _glob.glob(os.path.join(_OUTPUT_DIR, "video", "*.mp4"))
    status_files = _glob.glob(os.path.join(_OUTPUT_DIR, "video", "*_status.json"))
    if videos:
        results.append({
            "phase": "production",
            "valid": True,
            "message": f"{len(videos)} video clips produced ({len(status_files)} with QA)",
        })
    elif status_files:
        results.append({
            "phase": "production",
            "valid": False,
            "errors": f"QA status files exist ({len(status_files)}) but no MP4 files",
        })

    # Assembly: check for final documentary
    assembly_dir = os.path.join(_OUTPUT_DIR, "assembly")
    final_vids = _glob.glob(os.path.join(assembly_dir, "*.mp4")) if os.path.exists(assembly_dir) else []
    if final_vids:
        results.append({
            "phase": "assembly",
            "valid": True,
            "message": f"Final documentary assembled: {os.path.basename(final_vids[0])}",
        })

    return JSONResponse({"results": results})


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
