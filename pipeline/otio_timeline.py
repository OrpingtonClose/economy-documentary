"""
OTIO Timeline Manager -- standalone timeline operations for the pipeline.

This module provides a simplified interface to OpenTimelineIO for
creating and managing documentary timelines outside of ADK agent context.
Used by test_run.py and scripts for direct timeline manipulation.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import opentimelineio as otio

logger = logging.getLogger(__name__)

# Default timeline directory
TIMELINE_DIR = os.environ.get(
    "TIMELINE_DIR", "/tmp/documentary-pipeline/timelines"
)


def create_timeline(
    topic: str,
    num_scenes: int,
    output_dir: str = "",
) -> str:
    """Create a new OTIO timeline with the standard track structure.

    Tracks:
        - V1_Video: Video clips
        - A1_Narration: Narration audio clips
        - A2_Music: Background music clips

    Args:
        topic: Documentary topic name.
        num_scenes: Number of scenes (used for metadata).
        output_dir: Directory to save the OTIO file.

    Returns:
        Path to the created OTIO file.
    """
    out_dir = output_dir or TIMELINE_DIR
    os.makedirs(out_dir, exist_ok=True)

    timeline = otio.schema.Timeline(name=f"documentary_{topic}")
    timeline.metadata["topic"] = topic
    timeline.metadata["num_scenes"] = num_scenes

    # Create tracks
    video_track = otio.schema.Track(
        name="V1_Video",
        kind=otio.schema.TrackKind.Video,
    )
    narration_track = otio.schema.Track(
        name="A1_Narration",
        kind=otio.schema.TrackKind.Audio,
    )
    music_track = otio.schema.Track(
        name="A2_Music",
        kind=otio.schema.TrackKind.Audio,
    )

    timeline.tracks.append(video_track)
    timeline.tracks.append(narration_track)
    timeline.tracks.append(music_track)

    # Save
    safe_topic = topic.replace(" ", "_").replace("/", "_")[:50]
    filename = f"{safe_topic}.otio"
    filepath = os.path.join(out_dir, filename)
    otio.adapters.write_to_file(timeline, filepath)

    logger.info("Created timeline: %s (%d scenes)", filepath, num_scenes)
    return filepath


def load_timeline(filepath: str) -> otio.schema.Timeline:
    """Load an OTIO timeline from file."""
    return otio.adapters.read_from_file(filepath)


def save_timeline(timeline: otio.schema.Timeline, filepath: str) -> None:
    """Save an OTIO timeline to file."""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    otio.adapters.write_to_file(timeline, filepath)


def add_clip(
    filepath: str,
    track_name: str,
    clip_name: str,
    media_path: str,
    duration: float,
    metadata: Optional[dict] = None,
) -> bool:
    """Add a clip to a specific track with idempotency check.

    Args:
        filepath: Path to the OTIO file.
        track_name: Name of the track (e.g., "V1_Video").
        clip_name: Unique name for the clip.
        media_path: Path to the media file.
        duration: Clip duration in seconds.
        metadata: Optional metadata dict.

    Returns:
        True if clip was added, False if it already existed.
    """
    timeline = load_timeline(filepath)

    # Find track
    target_track = None
    for track in timeline.tracks:
        if track.name == track_name:
            target_track = track
            break

    if target_track is None:
        raise ValueError(f"Track '{track_name}' not found in timeline")

    # Idempotency check
    for item in target_track:
        if isinstance(item, otio.schema.Clip) and item.name == clip_name:
            logger.info("Clip '%s' already exists on '%s'", clip_name, track_name)
            return False

    # Create clip
    rate = 24.0
    source_range = otio.opentime.TimeRange(
        start_time=otio.opentime.RationalTime(0, rate),
        duration=otio.opentime.RationalTime(duration * rate, rate),
    )

    media_ref = otio.schema.ExternalReference(target_url=media_path)

    clip = otio.schema.Clip(
        name=clip_name,
        source_range=source_range,
        media_reference=media_ref,
    )

    if metadata:
        for k, v in metadata.items():
            clip.metadata[k] = v

    target_track.append(clip)
    save_timeline(timeline, filepath)

    logger.info("Added clip '%s' to '%s' (%.2fs)", clip_name, track_name, duration)
    return True


def get_timeline_summary(filepath: str) -> dict:
    """Get a summary of the timeline contents.

    Returns:
        Dict with track names, clip counts, and total durations.
    """
    timeline = load_timeline(filepath)

    tracks_summary = []
    for track in timeline.tracks:
        clips = []
        gaps = []
        for item in track:
            if isinstance(item, otio.schema.Clip):
                dur = item.source_range.duration.to_seconds() if item.source_range else 0
                clips.append({"name": item.name, "duration": round(dur, 3)})
            elif isinstance(item, otio.schema.Gap):
                gaps.append({"name": item.name or "gap"})

        tracks_summary.append({
            "name": track.name,
            "kind": str(track.kind),
            "total_clips": len(clips),
            "total_gaps": len(gaps),
            "clips": clips,
            "gaps": gaps,
        })

    return {
        "timeline_name": timeline.name,
        "tracks": tracks_summary,
    }


def validate_timeline(filepath: str, phase: str) -> dict:
    """Run phase-specific validation on the timeline.

    Args:
        filepath: Path to the OTIO file.
        phase: Pipeline phase ("scenario", "audio", "visual_direction",
               "production", "assembly").

    Returns:
        Dict with "valid" bool and optional "errors" string.
    """
    timeline = load_timeline(filepath)
    errors = []

    # Find tracks
    tracks_by_name = {t.name: t for t in timeline.tracks}

    if phase == "scenario":
        for required in ["V1_Video", "A1_Narration", "A2_Music"]:
            if required not in tracks_by_name:
                errors.append(f"Missing required track: {required}")

    elif phase == "audio":
        narration = tracks_by_name.get("A1_Narration")
        if narration is None:
            errors.append("A1_Narration track not found")
        else:
            for item in narration:
                if isinstance(item, otio.schema.Clip):
                    ref = item.media_reference
                    if isinstance(ref, otio.schema.ExternalReference):
                        wav_path = ref.target_url
                        if not os.path.exists(wav_path):
                            errors.append(f"WAV not found: {wav_path} (clip: {item.name})")

    elif phase == "production":
        video = tracks_by_name.get("V1_Video")
        if video is None:
            errors.append("V1_Video track not found")
        else:
            for item in video:
                if isinstance(item, otio.schema.Clip):
                    ref = item.media_reference
                    if isinstance(ref, otio.schema.ExternalReference):
                        mp4_path = ref.target_url
                        if not os.path.exists(mp4_path):
                            errors.append(f"MP4 not found: {mp4_path} (clip: {item.name})")

    elif phase == "assembly":
        # Check for gaps and sync
        video = tracks_by_name.get("V1_Video")
        narration = tracks_by_name.get("A1_Narration")
        if video and narration:
            v_clips = [i for i in video if isinstance(i, otio.schema.Clip)]
            a_clips = [i for i in narration if isinstance(i, otio.schema.Clip)]
            if not v_clips:
                errors.append("No video clips on V1_Video")
            if not a_clips:
                errors.append("No narration clips on A1_Narration")

    return {
        "valid": len(errors) == 0,
        "phase": phase,
        "errors": "; ".join(errors) if errors else None,
    }
