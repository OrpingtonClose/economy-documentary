from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

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
        self._lock = threading.RLock()

        # State
        self._otio_state: str = OTIO_STATE_DRAFT
        self._timeline: Any = None  # opentimelineio.schema.Timeline
        self._timeline_path: str = ""
        self._timeline_mtime: float = 0.0
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
    # Cache synchronization — the only place _timeline is reloaded from disk
    # -----------------------------------------------------------------------

    def refresh_from_disk(self) -> None:
        """Reload _timeline from the on-disk .otio file.

        This is the sole cache-synchronization wire and the only place the
        in-memory timeline is reloaded from disk.  It MUST be called before
        every read, mutation, or checkpoint so that in-memory state does not
        race with external writers (e.g., SyncOtioClient local fallback,
        A2A otio-agent, other processes).  Skipping this call before
        checkpoint serializes a ghost timeline to B2 and makes resume
        actively harmful.

        No-op if _timeline_path is empty or the file does not exist.
        """
        try:
            with self._lock:
                if not self._timeline_path:
                    return

                if os.path.exists(self._timeline_path):
                    import opentimelineio as otio
                    self._timeline = otio.adapters.read_from_file(self._timeline_path)
                    self._timeline_mtime = os.path.getmtime(self._timeline_path)
                    return

                json_path = os.path.splitext(self._timeline_path)[0] + ".json"
                if os.path.exists(json_path):
                    with open(json_path, "r") as f:
                        self._timeline = json.load(f)
                    self._timeline_mtime = os.path.getmtime(json_path)
        except Exception as exc:
            logger.error("refresh_from_disk failed: %s", exc)

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

                # Write to disk so contract enforcers and tools can find it
                os.makedirs(self._timeline_dir, exist_ok=True)
                otio.adapters.write_to_file(self._timeline, self._timeline_path)
                self._timeline_mtime = os.path.getmtime(self._timeline_path)
                logger.info("Timeline written to %s", self._timeline_path)
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
        The summary format depends on which stage is asking: audio, video,
        assembly, or scenario.
        """
        self.refresh_from_disk()
        with self._lock:
            if self._timeline is None:
                return "No timeline created yet."

            try:
                import opentimelineio as otio
            except ImportError:
                # Fallback text for dict mode
                tracks = self._timeline.get("tracks", {})
                lines = [f"Timeline: {self._timeline.get('name', 'unknown')}"]
                for tname, clips in tracks.items():
                    lines.append(f"  {tname}: {len(clips)} clips")
                return "\n".join(lines)

            lines = [f"Timeline: {self._timeline.name}"]
            for track in self._timeline.tracks:
                clip_count = len(track)
                kind = "video" if track.kind == otio.schema.Track.Kind.Video else "audio"
                lines.append(f"  {track.name} ({kind}): {clip_count} clips")
                if clip_count > 0:
                    for clip in track:
                        if hasattr(clip, "name"):
                            lines.append(f"    - {clip.name}")
            return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Mutation guards
    # -----------------------------------------------------------------------

    def guard_mutation(self, operation: str) -> None:
        """Raise if caller is not allowed to mutate."""
        with self._lock:
            if self._otio_state == OTIO_STATE_AUTHORITATIVE and self._escalation is None:
                raise OtioStateViolation(
                    f"Mutating authoritative OTIO without escalation: {operation}",
                    details={
                        "operation": operation,
                        "otio_state": self._otio_state,
                        "escalation": self._escalation,
                    },
                )

    # -----------------------------------------------------------------------
    # Timeline mutation
    # -----------------------------------------------------------------------

    def add_clip(self, track: str, scene_num: int, phrase_idx: int,
                 clip_path: str, duration: float, metadata: dict | None = None,
                 provenance: dict | None = None) -> None:
        """Add a clip to the specified track."""
        self.refresh_from_disk()
        self.guard_mutation("add_clip")
        with self._lock:
            try:
                import opentimelineio as otio
                clip = otio.schema.Clip(
                    name=f"s{scene_num}p{phrase_idx}",
                    source_range=otio.opentime.TimeRange(
                        start_time=otio.opentime.RationalTime(0, 24),
                        duration=otio.opentime.RationalTime(duration * 24, 24),
                    ),
                    media_reference=otio.schema.ExternalReference(
                        target_url=clip_path,
                        available_range=otio.opentime.TimeRange(
                            start_time=otio.opentime.RationalTime(0, 24),
                            duration=otio.opentime.RationalTime(duration * 24, 24),
                        ),
                    ),
                )
                if metadata:
                    clip.metadata.update(metadata)
                if provenance:
                    clip.metadata["provenance"] = provenance
                for t in self._timeline.tracks:
                    if t.name == track:
                        t.append(clip)
                        break
                self._write_timeline()
            except ImportError:
                tracks = self._timeline.get("tracks", {})
                if track in tracks:
                    tracks[track].append({
                        "scene_num": scene_num,
                        "phrase_idx": phrase_idx,
                        "path": clip_path,
                        "duration": duration,
                        "metadata": metadata or {},
                        "provenance": provenance or {},
                    })

    # -----------------------------------------------------------------------
    # Internal disk I/O
    # -----------------------------------------------------------------------

    def _write_timeline(self) -> None:
        """Write the current timeline to disk."""
        with self._lock:
            # Prevent clobbering a newer on-disk file with stale in-memory cache.
            if self._timeline_path:
                disk_path = self._timeline_path
                if not os.path.exists(disk_path):
                    disk_path = os.path.splitext(self._timeline_path)[0] + ".json"
                if os.path.exists(disk_path):
                    current_mtime = os.path.getmtime(disk_path)
                    if current_mtime != self._timeline_mtime:
                        logger.warning(
                            "_write_timeline: %s changed on disk since last refresh "
                            "(mtime %s != cached %s); reloading to avoid clobbering.",
                            disk_path, current_mtime, self._timeline_mtime,
                        )
                        self.refresh_from_disk()
                        return

            if isinstance(self._timeline, dict):
                # Dict mode — write JSON
                path = self._timeline_path.replace(".otio", ".json")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    json.dump(self._timeline, f, indent=2, default=str)
                self._timeline_mtime = os.path.getmtime(path)
                return

            try:
                import opentimelineio as otio
                if isinstance(self._timeline, otio.schema.Timeline) and self._timeline_path:
                    os.makedirs(os.path.dirname(self._timeline_path), exist_ok=True)
                    otio.adapters.write_to_file(self._timeline, self._timeline_path)
                    self._timeline_mtime = os.path.getmtime(self._timeline_path)
            except Exception as exc:
                logger.error("_write_timeline failed: %s", exc)
                raise

    # -----------------------------------------------------------------------
    # Pipeline metadata
    # -----------------------------------------------------------------------

    def set_pipeline_metadata(self, key: str, value: Any,
                               provenance: dict | None = None) -> None:
        """Set a key in timeline.metadata['documentary']."""
        self.refresh_from_disk()
        self.guard_mutation(f"set_pipeline_metadata:{key}")
        with self._lock:
            try:
                import opentimelineio as otio
                if isinstance(self._timeline, otio.schema.Timeline):
                    doc_meta = self._timeline.metadata.setdefault("documentary", {})
                    doc_meta[key] = {
                        "value": value,
                        "timestamp": time.time(),
                        "provenance": provenance or {},
                    }
                    self._write_timeline()
            except ImportError:
                if isinstance(self._timeline, dict):
                    self._timeline.setdefault("metadata", {}).setdefault("documentary", {})[key] = {
                        "value": value,
                        "timestamp": time.time(),
                        "provenance": provenance or {},
                    }
                    self._write_timeline()

    def get_pipeline_metadata(self, key: str, default: Any = None) -> Any:
        """Read a key from timeline.metadata['documentary']."""
        self.refresh_from_disk()
        with self._lock:
            try:
                import opentimelineio as otio
                if isinstance(self._timeline, otio.schema.Timeline):
                    meta = self._timeline.metadata
                    doc = meta.get("documentary", {})
                    entry = doc.get(key, default)
                    if isinstance(entry, dict) and "value" in entry:
                        return entry["value"]
                    return entry
            except ImportError:
                if isinstance(self._timeline, dict):
                    meta = self._timeline.get("metadata", {})
                    doc = meta.get("documentary", {})
                    entry = doc.get(key, default)
                    if isinstance(entry, dict) and "value" in entry:
                        return entry["value"]
                    return entry
            return default

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _record_transition(self, from_state: str | None, to_state: str, reason: str) -> None:
        """Record a state transition in the history."""
        self._history.append({
            "from": from_state,
            "to": to_state,
            "reason": reason,
            "timestamp": time.time(),
        })

    def _clip_counts(self) -> dict[str, int]:
        """Return clip counts per track."""
        counts: dict[str, int] = {}
        try:
            import opentimelineio as otio
            if isinstance(self._timeline, otio.schema.Timeline):
                for track in self._timeline.tracks:
                    counts[track.name] = len(track)
                return counts
        except ImportError:
            pass
        if isinstance(self._timeline, dict):
            for name, clips in self._timeline.get("tracks", {}).items():
                counts[name] = len(clips) if isinstance(clips, list) else 0
        return counts

    # -----------------------------------------------------------------------
    # Full state read
    # -----------------------------------------------------------------------

    def read_state(self) -> dict[str, Any]:
        """Return the full internal state for the orchestrator."""
        self.refresh_from_disk()
        with self._lock:
            counts = self._clip_counts()
            return {
                "otio_state": self._otio_state,
                "escalation": self._escalation,
                "history": list(self._history),
                "checkpoints": list(self._checkpoints),
                "clip_counts": counts,
                "cost_accrued": self._cost_accrued,
                "cost_budget": self._cost_budget,
                "qa_results": dict(self._qa_results),
                "navigation": dict(self._navigation),
                "timeline_path": self._timeline_path,
            }

    # -----------------------------------------------------------------------
    # Checkpoints — B2 and local
    # -----------------------------------------------------------------------