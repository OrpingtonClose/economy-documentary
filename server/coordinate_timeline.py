from __future__ import annotations

import sqlean
from collections import defaultdict
from typing import Any, Optional, Literal, cast
from pydantic import BaseModel, Field

from effects import (
    Effect,
    UpdateScript,
    MergeIntoOTIO,
    DurationAdjusted,
)
from projections import Projection

# Load the sqlite extensions for high-precision time calculations
sqlean.extensions.enable_all()

class IntervalSpan(BaseModel):
    """Represents a high-precision timespan on a timeline track."""
    start_sec: float
    end_sec: float

    def overlaps_with(self, other: IntervalSpan) -> bool:
        """Check if this timespan overlaps with another timespan."""
        return not (self.end_sec <= other.start_sec or self.start_sec >= other.end_sec)

    def duration(self) -> float:
        return self.end_sec - self.start_sec


class TimelineClip(BaseModel):
    """Represents a media clip anchored logically to a scenario block."""
    track_name: str
    span: IntervalSpan
    scenario_id: str = Field(..., description="Stable logical foreign key anchor to Scenario Block ID")
    version_hash: str = Field(..., description="Version/Attempt identifier")
    artifact_uri: Optional[str] = None


class CoordinateTimeline(Projection):
    """Rebuilds a coordinate-based timeline of clips, enforcing overlap constraints.

    Uses `sqlean` SQL backend to showcase native SQL interval operations.
    """

    def __init__(self) -> None:
        super().__init__()
        # track_name -> list of clips
        self.clips: dict[str, list[TimelineClip]] = defaultdict(list)
        # scenario_id -> target/measured duration
        self.scenario_durations: dict[str, float] = {}
        # Ordered list of scenario IDs representing screenplay draft order
        self.scenario_order: list[str] = []

    def apply(self, event: Effect) -> None:
        if event.kind == "update_script":
            self._apply_script(cast(UpdateScript, event))
        elif event.kind == "merge_into_otio":
            self._apply_merge(cast(MergeIntoOTIO, event))
        elif event.kind == "duration_adjusted":
            self._apply_duration_adjust(cast(DurationAdjusted, event))

    def _apply_script(self, event: UpdateScript) -> None:
        """Process screenplay draft, establishing base target durations and order."""
        self.scenario_durations = {b.block_id: b.duration_sec for b in event.blocks}
        self.scenario_order = [b.block_id for b in event.blocks]
        self._recalculate_offsets()

    def _apply_merge(self, event: MergeIntoOTIO) -> None:
        """Attempt to merge a clip at a timespan coordinate. Enforces overlap constraints."""
        start_sec = getattr(event, "start_sec", 0.0)
        # In a coordinate-based model, if start_sec is not provided, we fall back to computed offset
        if start_sec == 0.0 and event.block_id in self.scenario_durations:
            # Calculate from scenario offset mapping
            start_sec = self._get_scenario_offset(event.block_id)

        duration_sec = event.duration_sec
        new_span = IntervalSpan(start_sec=start_sec, end_sec=start_sec + duration_sec)
        
        new_clip = TimelineClip(
            track_name=event.track_name,
            span=new_span,
            scenario_id=event.block_id,
            version_hash=event.job_id,
            artifact_uri=event.artifact_uri
        )

        # STRICT OVERLAP EXCLUSION CHECK
        for clip in self.clips[event.track_name]:
            if clip.scenario_id != new_clip.scenario_id and clip.span.overlaps_with(new_clip.span):
                raise ValueError(
                    f"Collision on track '{event.track_name}': "
                    f"New clip '{new_clip.scenario_id}' at [{new_clip.span.start_sec:.3f}s - {new_clip.span.end_sec:.3f}s] "
                    f"overlaps with existing clip '{clip.scenario_id}' at [{clip.span.start_sec:.3f}s - {clip.span.end_sec:.3f}s]"
                )

        # Insert or update in-memory list
        self._upsert_clip(new_clip)

    def _apply_duration_adjust(self, event: DurationAdjusted) -> None:
        """Update measured duration of a scenario anchor, cascading recalculations downstream."""
        block_id = event.block_id
        # DurationAdjusted contains block_id which is the logical Scenario key (e.g. A1:1:s1_b1 -> s1_b1)
        clean_block_id = block_id.split(":")[-1]
        self.scenario_durations[clean_block_id] = event.measured_sec
        self._recalculate_offsets()

    def _upsert_clip(self, new_clip: TimelineClip) -> None:
        track = new_clip.track_name
        for i, clip in enumerate(self.clips[track]):
            if clip.scenario_id == new_clip.scenario_id:
                self.clips[track][i] = new_clip
                return
        self.clips[track].append(new_clip)

    def _get_scenario_offset(self, target_block_id: str) -> float:
        cursor = 0.0
        for block_id in self.scenario_order:
            if block_id == target_block_id:
                return cursor
            cursor += self.scenario_durations.get(block_id, 3.0)
        return cursor

    def _recalculate_offsets(self) -> None:
        """Cascade shift: re-calculate coordinate starts and ends for all anchored clips."""
        cursor = 0.0
        for block_id in self.scenario_order:
            dur = self.scenario_durations.get(block_id, 3.0)
            span = IntervalSpan(start_sec=cursor, end_sec=cursor + dur)
            
            # Reposition any existing clips bound to this logical block
            for track_clips in self.clips.values():
                for clip in track_clips:
                    if clip.scenario_id == block_id:
                        clip.span = span
            cursor += dur

    def add_clip(self, track_name: str, clip_id: str, start_sec: float, duration_sec: float, artifact_uri: Optional[str] = None) -> None:
        new_span = IntervalSpan(start_sec=start_sec, end_sec=start_sec + duration_sec)
        new_clip = TimelineClip(
            track_name=track_name,
            span=new_span,
            scenario_id=clip_id,
            version_hash="v1",
            artifact_uri=artifact_uri
        )
        self._upsert_clip(new_clip)

    def get_spans(self, track_name: str) -> list[IntervalSpan]:
        sorted_clips = sorted(self.clips.get(track_name, []), key=lambda c: c.span.start_sec)
        return [clip.span for clip in sorted_clips]

    def query_sqlean_timespan(self, start: float, duration: float) -> int:
        """Prove sqlean-time loading and precision interval subtract in SQL database.

        Returns calculated duration in nanoseconds using database-native time functions.
        """
        conn = sqlean.connect(':memory:')
        sqlean.extensions.enable_all()
        
        # Format floating-point seconds into high-precision dates/times and calculate the difference
        # using the sqlean 'time_sub' function
        val_start = int(start)
        ns_start = int((start - val_start) * 1000000000)
        
        val_end = int(start + duration)
        ns_end = int(((start + duration) - val_end) * 1000000000)

        query = """
            SELECT time_sub(
                time_date(2026, 6, 2, 12, 0, ?, ?),
                time_date(2026, 6, 2, 12, 0, ?, ?)
            )
        """
        res = conn.execute(query, (val_end, ns_end, val_start, ns_start)).fetchone()
        conn.close()
        return res[0]
