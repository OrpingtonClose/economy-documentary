"""
OTIO timeline tools -- ADK FunctionTool wrappers for OpenTimelineIO operations.

All timeline mutations go through these tools. They enforce idempotency
(check for existing clips before appending) and maintain the canonical
track structure: V1_Video, A1_Narration, A2_Music.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import opentimelineio as otio

from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

# Canonical track names
TRACK_V1 = "V1_Video"
TRACK_A1 = "A1_Narration"
TRACK_A2 = "A2_Music"

_TIMELINE_DIR = os.environ.get("TIMELINE_DIR", "/tmp/documentary-pipeline/timelines")


def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def _timeline_path(topic: str) -> str:
    os.makedirs(_TIMELINE_DIR, exist_ok=True)
    safe_topic = topic.replace(" ", "_").replace("/", "_")[:50]
    return os.path.join(_TIMELINE_DIR, f"{safe_topic}.otio")


def create_timeline(
    topic: str,
    num_scenes: int,
    tool_context=None,
) -> str:
    """Create a new OTIO timeline with the canonical track structure.

    Args:
        topic: Documentary topic name.
        num_scenes: Number of scenes to create placeholder gaps for.

    Returns:
        JSON string with timeline path and structure summary.
    """
    timeline = otio.schema.Timeline(name=f"Documentary: {topic}")

    # Create video track with gaps for each scene
    video_track = otio.schema.Track(name=TRACK_V1, kind=otio.schema.TrackKind.Video)
    for i in range(1, num_scenes + 1):
        gap = otio.schema.Gap(
            name=f"scene_{i:03d}_video",
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(0, 24),
            ),
        )
        gap.metadata["documentary"] = {"scene_num": i, "status": "empty"}
        video_track.append(gap)

    # Create narration track
    narration_track = otio.schema.Track(
        name=TRACK_A1, kind=otio.schema.TrackKind.Audio
    )

    # Create music track
    music_track = otio.schema.Track(
        name=TRACK_A2, kind=otio.schema.TrackKind.Audio
    )

    timeline.tracks.append(video_track)
    timeline.tracks.append(narration_track)
    timeline.tracks.append(music_track)

    path = _timeline_path(topic)
    _ensure_dir(path)
    otio.adapters.write_to_file(timeline, path)

    # Store path in tool_context state if available
    if tool_context:
        tool_context.state["_timeline_path"] = path

    logger.info("Created OTIO timeline: %s (%d scenes)", path, num_scenes)

    return json.dumps(
        {
            "timeline_path": path,
            "tracks": [TRACK_V1, TRACK_A1, TRACK_A2],
            "num_scenes": num_scenes,
            "status": "created",
        }
    )


def add_narration_clip(
    scene_num: int,
    voice: str,
    wav_path: str,
    duration: float,
    tool_context=None,
) -> str:
    """Add a narration clip to A1_Narration track.

    Idempotent: checks for existing clip with same scene_num + voice
    before appending.

    Args:
        scene_num: Scene number (1-based).
        voice: Voice role identifier (e.g., "V1", "V2", "V3").
        wav_path: Path to the generated WAV file.
        duration: Duration in seconds.

    Returns:
        JSON string with clip details.
    """
    state = tool_context.state if tool_context else {}
    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        return json.dumps({"error": "Timeline not found. Create one first."})

    timeline = otio.adapters.read_from_file(timeline_path)
    narration_track = None
    for track in timeline.tracks:
        if track.name == TRACK_A1:
            narration_track = track
            break

    if not narration_track:
        return json.dumps({"error": "A1_Narration track not found"})

    clip_name = f"scene_{scene_num:03d}_{voice}"

    # Idempotency check: don't add duplicates
    for item in narration_track:
        if isinstance(item, otio.schema.Clip) and item.name == clip_name:
            return json.dumps(
                {
                    "status": "already_exists",
                    "clip_name": clip_name,
                    "message": "Clip already exists, skipping duplicate",
                }
            )

    clip = otio.schema.Clip(
        name=clip_name,
        media_reference=otio.schema.ExternalReference(target_url=wav_path),
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, 24),
            duration=otio.opentime.RationalTime(duration * 24, 24),
        ),
    )
    clip.metadata["documentary"] = {
        "scene_num": scene_num,
        "voice": voice,
        "type": "narration",
    }

    narration_track.append(clip)
    otio.adapters.write_to_file(timeline, timeline_path)

    logger.info("Added narration clip: %s (%.2fs)", clip_name, duration)
    return json.dumps(
        {
            "status": "added",
            "clip_name": clip_name,
            "duration": duration,
            "wav_path": wav_path,
        }
    )


def add_video_clip(
    scene_num: int,
    phrase_idx: int,
    mp4_path: str,
    duration: float,
    source_range: float,
    available_range: float,
    lora_id: str,
    tool_context=None,
) -> str:
    """Add a video clip to V1_Video track.

    Idempotent: checks for existing clip with same scene_num + phrase_idx.

    Args:
        scene_num: Scene number (1-based).
        phrase_idx: Visual phrase index within the scene.
        mp4_path: Path to the generated MP4 file.
        duration: Target duration in seconds.
        source_range: Source range duration in seconds.
        available_range: Available range duration in seconds.
        lora_id: LoRA identifier used for generation.

    Returns:
        JSON string with clip details.
    """
    state = tool_context.state if tool_context else {}
    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        return json.dumps({"error": "Timeline not found. Create one first."})

    timeline = otio.adapters.read_from_file(timeline_path)
    video_track = None
    for track in timeline.tracks:
        if track.name == TRACK_V1:
            video_track = track
            break

    if not video_track:
        return json.dumps({"error": "V1_Video track not found"})

    clip_name = f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}"

    # Idempotency check
    for item in video_track:
        if isinstance(item, otio.schema.Clip) and item.name == clip_name:
            return json.dumps(
                {
                    "status": "already_exists",
                    "clip_name": clip_name,
                    "message": "Clip already exists, skipping duplicate",
                }
            )

    clip = otio.schema.Clip(
        name=clip_name,
        media_reference=otio.schema.ExternalReference(target_url=mp4_path),
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, 24),
            duration=otio.opentime.RationalTime(source_range * 24, 24),
        ),
    )
    clip.metadata["documentary"] = {
        "scene_num": scene_num,
        "phrase_idx": phrase_idx,
        "lora_id": lora_id,
        "available_range": available_range,
        "type": "video",
    }

    # Replace the corresponding gap or append
    replaced = False
    for i, item in enumerate(video_track):
        if isinstance(item, otio.schema.Gap):
            gap_meta = item.metadata.get("documentary", {})
            if gap_meta.get("scene_num") == scene_num:
                video_track[i] = clip
                replaced = True
                break

    if not replaced:
        video_track.append(clip)

    otio.adapters.write_to_file(timeline, timeline_path)

    logger.info("Added video clip: %s (%.2fs)", clip_name, duration)
    return json.dumps(
        {
            "status": "added",
            "clip_name": clip_name,
            "duration": duration,
            "source_range": source_range,
            "available_range": available_range,
            "lora_id": lora_id,
        }
    )


def get_timeline_status(tool_context=None) -> str:
    """Get a summary of all tracks, clips, and gaps in the timeline.

    Returns:
        JSON string with timeline structure summary.
    """
    state = tool_context.state if tool_context else {}
    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        return json.dumps({"error": "Timeline not found."})

    timeline = otio.adapters.read_from_file(timeline_path)

    tracks_info = []
    for track in timeline.tracks:
        clips = []
        gaps = []
        for item in track:
            if isinstance(item, otio.schema.Clip):
                clips.append(
                    {
                        "name": item.name,
                        "duration": (
                            item.source_range.duration.to_seconds()
                            if item.source_range
                            else 0
                        ),
                        "metadata": dict(item.metadata.get("documentary", {})),
                    }
                )
            elif isinstance(item, otio.schema.Gap):
                gaps.append(
                    {
                        "name": item.name,
                        "metadata": dict(item.metadata.get("documentary", {})),
                    }
                )

        tracks_info.append(
            {
                "name": track.name,
                "kind": str(track.kind),
                "clips": clips,
                "gaps": gaps,
                "total_clips": len(clips),
                "total_gaps": len(gaps),
            }
        )

    return json.dumps(
        {"timeline_name": timeline.name, "tracks": tracks_info},
        indent=2,
    )


def validate_timeline(phase: str, tool_context=None) -> str:
    """Run phase-specific validation checks on the timeline.

    Args:
        phase: Pipeline phase name (scenario, audio, visual_direction,
               production, assembly).

    Returns:
        JSON string with validation results.
    """
    from callbacks.timeline_guardian import _VALIDATORS, _load_timeline

    state = tool_context.state if tool_context else {}
    timeline = _load_timeline(state)

    if not timeline:
        return json.dumps({"valid": False, "error": "Timeline not found"})

    validator = _VALIDATORS.get(phase)
    if not validator:
        return json.dumps(
            {"valid": False, "error": f"Unknown phase: {phase}"}
        )

    error = validator(timeline, state)
    if error:
        return json.dumps({"valid": False, "phase": phase, "errors": error})

    return json.dumps({"valid": True, "phase": phase, "message": "All checks passed"})


# -- ADK FunctionTool wrappers -------------------------------------------------
create_timeline_tool = FunctionTool(create_timeline)
add_narration_clip_tool = FunctionTool(add_narration_clip)
add_video_clip_tool = FunctionTool(add_video_clip)
get_timeline_status_tool = FunctionTool(get_timeline_status)
validate_timeline_tool = FunctionTool(validate_timeline)

otio_tools = [
    create_timeline_tool,
    add_narration_clip_tool,
    add_video_clip_tool,
    get_timeline_status_tool,
    validate_timeline_tool,
]
