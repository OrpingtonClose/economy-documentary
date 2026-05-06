"""
OTIO State Manager — single source of truth for timeline state.

The OTIO timeline is a complex Python object (not JSON-serializable),
so it cannot live directly in the Strands Graph's ``invocation_state``.
Instead, this manager holds the timeline reference and is passed *into*
the invocation_state as a pointer. The LLM accesses timeline data via
``otio_read(stage)`` which returns a text summary, not the raw object.

Architecture::

    OTIOStateManager (separate from invocation_state)
      ├─ Holds: Timeline, _navigation, FeedbackStore, QA, cost
      ├─ Passed via invocation_state as reference
      ├─ Checkpoints via otio_json → B2 after each stage
      └─ LLM access via otio_read(stage) → text summary

This replaces the blackboard dict pattern from the ADK pipeline where
OTIO objects were crammed into session state and the LLM couldn't
see them anyway.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OTIO_STATE_DRAFT = "draft"
OTIO_STATE_AUTHORITATIVE = "authoritative"
VALID_STATES = frozenset({OTIO_STATE_DRAFT, OTIO_STATE_AUTHORITATIVE})

TRACK_V1 = "V1_Video"
TRACK_A1 = "A1_Narration"
TRACK_A2 = "A2_Music"
CANONICAL_TRACKS = [TRACK_V1, TRACK_A1, TRACK_A2]

# ---------------------------------------------------------------------------
# Structured failure
# ---------------------------------------------------------------------------


class OtioStateViolation(RuntimeError):
    """Raised when a caller tries to mutate an authoritative OTIO timeline.

    Carries a structured ``details`` dict so log/telemetry consumers can
    reason about the violation without parsing the message string.
    """

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


# ---------------------------------------------------------------------------
# OTIO State Manager
# ---------------------------------------------------------------------------


class OTIOStateManager:
    """Single source of truth for the documentary timeline.

    Holds the OTIO timeline object, its lifecycle state (draft/authoritative),
    navigation metadata, and checkpoint history. The LLM can only read
    text summaries; all mutations go through guard methods.

    Usage::

        mgr = OTIOStateManager(output_dir="/tmp/pipeline")
        mgr.create_timeline("documentary_draft")
        mgr.add_narration_clip(scene_num=1, phrase_idx=0, path="s1p0.wav", duration=5.2)
        summary = mgr.read("audio")  # text summary for LLM
        mgr.checkpoint("after_audio")  # serialize to B2
        mgr.set_authoritative("narration_reconciliation_complete")
    """

    def __init__(self, output_dir: str = "/tmp/documentary-pipeline") -> None:
        self._output_dir = output_dir
        self._timeline_dir = os.path.join(output_dir, "timelines")
        self._lock = threading.Lock()

        # State
        self._otio_state: str = OTIO_STATE_DRAFT
        self._timeline: Any = None  # opentimelineio.schema.Timeline
        self._timeline_path: str = ""
        self._navigation: dict[str, Any] = {}
        self._escalation: dict[str, Any] | None = None
        self._history: list[dict[str, Any]] = []
        self._checkpoints: list[dict[str, Any]] = []

        # Cost tracking
        self._cost_accrued: float = 0.0
        self._cost_budget: float = 0.0

        # QA results per stage
        self._qa_results: dict[str, list[dict[str, Any]]] = {}

    # -----------------------------------------------------------------------
    # Lifecycle state
    # -----------------------------------------------------------------------

    @property
    def state(self) -> str:
        """Current OTIO lifecycle state: 'draft' or 'authoritative'."""
        return self._otio_state

    @property
    def is_authoritative(self) -> bool:
        return self._otio_state == OTIO_STATE_AUTHORITATIVE

    def set_authoritative(self, reason: str) -> None:
        """Transition draft → authoritative. Only valid from draft."""
        with self._lock:
            if self._otio_state == OTIO_STATE_AUTHORITATIVE:
                return  # idempotent
            self._otio_state = OTIO_STATE_AUTHORITATIVE
            self._record_transition(OTIO_STATE_DRAFT, OTIO_STATE_AUTHORITATIVE, reason)
            logger.info("OTIO state → authoritative: %s", reason)

    def reset_to_draft(self, reason: str) -> None:
        """Transition back to draft (only during escalation)."""
        with self._lock:
            if self._otio_state == OTIO_STATE_DRAFT:
                return
            if self._escalation is None:
                raise OtioStateViolation(
                    "Cannot reset to draft without an active escalation",
                    details={
                        "operation": "reset_to_draft",
                        "otio_state": self._otio_state,
                        "escalation": None,
                    },
                )
            self._otio_state = OTIO_STATE_DRAFT
            self._record_transition(OTIO_STATE_AUTHORITATIVE, OTIO_STATE_DRAFT, reason)
            logger.info("OTIO state → draft (escalation): %s", reason)

    def begin_escalation(self, escalation_type: str, reason: str, opened_by: str) -> None:
        """Open an escalation window that allows mutation of authoritative OTIO."""
        with self._lock:
            self._escalation = {
                "type": escalation_type,
                "reason": reason,
                "opened_by": opened_by,
                "timestamp": time.time(),
            }
            logger.info("Escalation opened: %s (%s) by %s", escalation_type, reason, opened_by)

    def end_escalation(self) -> None:
        """Close the escalation window."""
        with self._lock:
            self._escalation = None

    @property
    def escalation(self) -> dict[str, Any] | None:
        return self._escalation

    # -----------------------------------------------------------------------
    # Timeline creation
    # -----------------------------------------------------------------------

    def create_timeline(self, name: str = "documentary_draft") -> None:
        """Create a new OTIO timeline with the canonical track structure.

        Uses opentimelineio if available; falls back to a lightweight dict
        representation for testing without the OTIO dependency.
        """
        with self._lock:
            self._timeline_path = os.path.join(self._timeline_dir, f"{name}.otio")

            try:
                import opentimelineio as otio

                self._timeline = otio.schema.Timeline(name=name)
                for track_name in CANONICAL_TRACKS:
                    kind = "video" if track_name == TRACK_V1 else "audio"
                    track = otio.schema.Track(name=track_name, kind=kind)
                    self._timeline.tracks.append(track)
            except ImportError:
                # Lightweight fallback for environments without OTIO
                logger.debug("opentimelineio not available, using dict timeline")
                self._timeline = {
                    "name": name,
                    "tracks": {
                        track_name: [] for track_name in CANONICAL_TRACKS
                    },
                    "metadata": {},
                }

            os.makedirs(self._timeline_dir, exist_ok=True)
            self._record_transition(None, OTIO_STATE_DRAFT, "timeline_created")

    # -----------------------------------------------------------------------
    # Read access (for LLM tools)
    # -----------------------------------------------------------------------

    def read(self, stage: str) -> str:
        """Return a text summary of the timeline for a given stage.

        This is what the LLM sees — not the raw OTIO object.
        The summary format depends on which stage is asking:
        - scenario: scene list + durations
        - audio: narration clips + durations + alignment
        - visual: visual concepts + clip status
        - production: rendered clips + QA scores
        - assembly: final output status
        """
        with self._lock:
            if self._timeline is None:
                return "No timeline created yet."

            lines = [f"Timeline state: {self._otio_state}"]
            if self._escalation:
                lines.append(f"Escalation: {self._escalation['type']} ({self._escalation['reason']})")

            # Count clips per track
            try:
                import opentimelineio as otio

                if isinstance(self._timeline, otio.schema.Timeline):
                    for track in self._timeline.tracks:
                        clip_count = len(track)
                        lines.append(f"  {track.name}: {clip_count} clips")
                else:
                    raise ImportError
            except (ImportError, TypeError):
                # Dict fallback
                if isinstance(self._timeline, dict):
                    tracks = self._timeline.get("tracks", {})
                    for track_name, clips in tracks.items():
                        lines.append(f"  {track_name}: {len(clips)} clips")

            # Stage-specific details
            if stage in self._qa_results:
                lines.append(f"  QA results ({stage}): {len(self._qa_results[stage])} checks")

            lines.append(f"  Cost accrued: ${self._cost_accrued:.2f} / ${self._cost_budget:.2f}")

            return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Mutations (guarded when authoritative)
    # -----------------------------------------------------------------------

    def guard_mutation(self, operation: str) -> None:
        """Check if a mutation is allowed.

        Raises OtioStateViolation if the timeline is authoritative
        and no escalation is active.
        """
        if not self.is_authoritative:
            return  # Draft — mutations always allowed
        if self._escalation is not None:
            return  # Escalation window — mutations allowed
        raise OtioStateViolation(
            f"Cannot perform '{operation}' on authoritative timeline",
            details={
                "operation": operation,
                "otio_state": self._otio_state,
                "timeline_path": self._timeline_path,
                "escalation": self._escalation,
            },
        )

    def add_clip(self, track: str, scene_num: int, phrase_idx: int,
                 clip_path: str, duration: float, metadata: dict | None = None) -> None:
        """Add a clip to the specified track. Guarded when authoritative."""
        self.guard_mutation("add_clip")
        with self._lock:
            if isinstance(self._timeline, dict):
                tracks = self._timeline.setdefault("tracks", {})
                track_clips = tracks.setdefault(track, [])
                clip = {
                    "scene_num": scene_num,
                    "phrase_idx": phrase_idx,
                    "path": clip_path,
                    "duration": duration,
                    "metadata": metadata or {},
                    "added_at": time.time(),
                }
                track_clips.append(clip)
            else:
                # Real OTIO timeline — add clip to the matching track
                import opentimelineio as otio
                for t in self._timeline.tracks:
                    if t.name == track:
                        clip = otio.schema.Clip(
                            name=f"scene_{scene_num}_phrase_{phrase_idx}",
                            source_range=otio.opentime.TimeRange(
                                start_time=otio.opentime.RationalTime(0, 24),
                                duration=otio.opentime.RationalTime.from_seconds(duration, 24),
                            ),
                        )
                        clip.media_reference = otio.schema.ExternalReference(
                            target_url=clip_path,
                        )
                        if metadata:
                            clip.metadata["documentary"] = metadata
                        t.append(clip)
                        break
                else:
                    logger.warning("Track '%s' not found in timeline", track)

    # -----------------------------------------------------------------------
    # Checkpoints
    # -----------------------------------------------------------------------

    def checkpoint(self, label: str) -> dict[str, Any]:
        """Serialize the current timeline state and record a checkpoint.

        Returns the checkpoint dict for B2 upload.
        """
        with self._lock:
            checkpoint = {
                "label": label,
                "otio_state": self._otio_state,
                "timeline_path": self._timeline_path,
                "timestamp": time.time(),
                "cost_accrued": self._cost_accrued,
                "qa_summary": {
                    stage: len(checks) for stage, checks in self._qa_results.items()
                },
                "clip_counts": self._clip_counts(),
            }

            # If OTIO is available, serialize via otio_json
            try:
                import opentimelineio as otio

                if isinstance(self._timeline, otio.schema.Timeline):
                    checkpoint["otio_json"] = otio.adapters.otio_json.write_to_string(
                        self._timeline
                    )
            except (ImportError, Exception):
                # Dict fallback — serialize as-is
                if isinstance(self._timeline, dict):
                    checkpoint["timeline_dict"] = self._timeline

            self._checkpoints.append(checkpoint)
            logger.info("Checkpoint '%s': %s", label, checkpoint.get("clip_counts", {}))
            return checkpoint

    def _clip_counts(self) -> dict[str, int]:
        """Count clips per track."""
        counts = {}
        if isinstance(self._timeline, dict):
            for track_name, clips in self._timeline.get("tracks", {}).items():
                counts[track_name] = len(clips)
        else:
            # Real OTIO timeline
            import opentimelineio as otio
            for track in self._timeline.tracks:
                clip_count = sum(1 for item in track if isinstance(item, otio.schema.Clip))
                if track.name:
                    counts[track.name] = clip_count
        return counts

    # -----------------------------------------------------------------------
    # QA + Cost
    # -----------------------------------------------------------------------

    def record_qa(self, stage: str, result: dict[str, Any]) -> None:
        """Record a QA check result for a stage."""
        with self._lock:
            self._qa_results.setdefault(stage, []).append(result)

    def add_cost(self, amount: float) -> None:
        """Add to the accrued cost."""
        self._cost_accrued += amount

    @property
    def cost(self) -> tuple[float, float]:
        """Return (accrued, budget) cost tuple."""
        return (self._cost_accrued, self._cost_budget)

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _record_transition(self, from_state: str | None, to_state: str, reason: str) -> None:
        self._history.append({
            "from": from_state,
            "to": to_state,
            "reason": reason,
            "timestamp": time.time(),
        })

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    @property
    def checkpoints(self) -> list[dict[str, Any]]:
        return list(self._checkpoints)
