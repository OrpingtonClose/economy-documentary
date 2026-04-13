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
import threading
from typing import Optional

import opentimelineio as otio

from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

# Canonical track names
TRACK_V1 = "V1_Video"
TRACK_A1 = "A1_Narration"
TRACK_A2 = "A2_Music"

_TIMELINE_DIR = os.environ.get("TIMELINE_DIR", "/tmp/documentary-pipeline/timelines")

# Module-level lock to protect OTIO file read-modify-write cycles against
# concurrent tool calls (parallel_tool_calls=True is the default).
_otio_lock = threading.Lock()


def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def _timeline_path(topic: str) -> str:
    os.makedirs(_TIMELINE_DIR, exist_ok=True)
    # Sanitise all characters that are problematic in file paths
    safe_topic = topic
    for ch in " /:\\?*\"<>|'":
        safe_topic = safe_topic.replace(ch, "_")
    safe_topic = safe_topic[:50]
    return os.path.join(_TIMELINE_DIR, f"{safe_topic}.otio")


def create_timeline(
    topic: str,
    num_scenes: int,
    tool_context=None,
) -> str:
    """Create a new OTIO timeline with the canonical track structure.

    Idempotent: if ``_timeline_path`` already exists in the session state
    the call is a no-op and returns the existing path.  This prevents the
    scenario generator from overwriting the timeline on evaluator-loop
    re-runs inside the LoopAgent.

    Args:
        topic: Documentary topic name.
        num_scenes: Number of scenes to create placeholder gaps for.

    Returns:
        JSON string with timeline path and structure summary.
    """
    # Guard: skip re-creation if timeline already exists in session state
    if tool_context:
        existing_path = tool_context.state.get("_timeline_path", "")
        if existing_path and os.path.exists(existing_path):
            logger.info(
                "Timeline already exists at %s — skipping re-creation",
                existing_path,
            )
            return json.dumps(
                {
                    "timeline_path": existing_path,
                    "tracks": [TRACK_V1, TRACK_A1, TRACK_A2],
                    "num_scenes": num_scenes,
                    "status": "already_exists",
                }
            )

    with _otio_lock:
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

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)
        narration_track = None
        for track in timeline.tracks:
            if track.name == TRACK_A1:
                narration_track = track
                break

        if narration_track is None:
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


def get_narration_durations_by_scene(tool_context=None) -> dict:
    """Read the OTIO timeline and return narration durations per scene.

    Returns a dict mapping scene_num -> list of (voice, duration_sec) tuples,
    ordered as they appear on the A1_Narration track.

    This is the AUTHORITATIVE source for how long video clips must be.
    Video concepts MUST be sized to match these durations.
    """
    state = tool_context.state if tool_context else {}
    timeline_path = state.get("_timeline_path", "")
    if not timeline_path or not os.path.exists(timeline_path):
        return {}

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)

    narration_track = None
    for track in timeline.tracks:
        if track.name == TRACK_A1:
            narration_track = track
            break

    if narration_track is None:
        return {}

    result: dict[int, list[tuple[str, float]]] = {}
    for item in narration_track:
        if isinstance(item, otio.schema.Clip) and item.source_range:
            meta = item.metadata.get("documentary", {})
            sn = meta.get("scene_num", 0)
            voice = meta.get("voice", "")
            dur = item.source_range.duration.to_seconds()
            if sn > 0 and dur > 0:
                result.setdefault(sn, []).append((voice, dur))

    return result


def _gatekeeper_check_video_clip(
    timeline,
    scene_num: int,
    phrase_idx: int,
    source_range: float,
) -> Optional[str]:
    """OTIO Gatekeeper: validate a video clip BEFORE it is added to the timeline.

    Checks:
    1. source_range > 0
    2. source_range <= available narration duration for this scene
    3. phrase_idx corresponds to a real narration phrase (not phantom)

    Returns None if valid, or an error string if the clip must be rejected.
    """
    if source_range <= 0:
        return (
            f"source_range={source_range:.3f}s for scene {scene_num} "
            f"phrase {phrase_idx} — must be > 0"
        )

    # Cross-check against narration track
    narration_track = None
    for track in timeline.tracks:
        if track.name == TRACK_A1:
            narration_track = track
            break

    if narration_track is None:
        # No narration yet — cannot cross-validate.  This is acceptable
        # only if audio stage hasn't run (production should not run before
        # audio, but we don't block here — the contract validator handles
        # stage ordering).
        return None

    # Collect narration clips for this scene (exclude alternate language
    # suffixes — match primary language only for video sizing).
    scene_narrations: list[tuple[str, float]] = []
    for item in narration_track:
        if isinstance(item, otio.schema.Clip) and item.source_range:
            meta = item.metadata.get("documentary", {})
            sn = meta.get("scene_num", 0)
            voice = meta.get("voice", "")
            # Skip alternate language clips (e.g. V1_EN in dual mode)
            if sn == scene_num and not voice.endswith("_EN"):
                dur = item.source_range.duration.to_seconds()
                scene_narrations.append((voice, dur))

    if not scene_narrations:
        # No narration for this scene — unusual but not gatekeeper's job
        # to enforce stage ordering.
        return None

    # Check phrase_idx is within bounds
    if phrase_idx >= len(scene_narrations):
        return (
            f"phrase_idx={phrase_idx} for scene {scene_num} exceeds "
            f"narration phrase count ({len(scene_narrations)}). "
            f"Video must have exactly one clip per narration phrase."
        )

    # Check source_range matches corresponding narration duration (1s tolerance)
    expected_dur = scene_narrations[phrase_idx][1]
    if abs(source_range - expected_dur) > 1.0:
        return (
            f"scene {scene_num} phrase {phrase_idx}: video source_range "
            f"({source_range:.2f}s) does not match narration duration "
            f"({expected_dur:.2f}s) — drift {abs(source_range - expected_dur):.2f}s > 1s. "
            f"Video clips must be sized to match narration."
        )

    return None


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

    GATEKEEPER: Before adding, cross-validates the clip's source_range
    against the narration track to ensure video-audio timing consistency.
    Rejects clips that would create a duration mismatch.

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

    with _otio_lock:
        timeline = otio.adapters.read_from_file(timeline_path)
        video_track = None
        for track in timeline.tracks:
            if track.name == TRACK_V1:
                video_track = track
                break

        if video_track is None:
            return json.dumps({"error": "V1_Video track not found"})

        # GATEKEEPER: cross-validate against narration before adding
        gate_error = _gatekeeper_check_video_clip(
            timeline, scene_num, phrase_idx, source_range,
        )
        if gate_error:
            logger.error(
                "OTIO GATEKEEPER REJECT: video clip scene %d phrase %d: %s",
                scene_num, phrase_idx, gate_error,
            )
            return json.dumps({"error": f"OTIO GATEKEEPER: {gate_error}"})

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
            media_reference=otio.schema.ExternalReference(
                target_url=mp4_path,
                available_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, 24),
                    duration=otio.opentime.RationalTime(available_range * 24, 24),
                ),
            ),
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

        # Insert clip in correct sorted position by (scene_num, phrase_idx).
        # First, try replacing the scene's placeholder gap (only for phrase 0).
        replaced = False
        if phrase_idx == 0:
            for i, item in enumerate(video_track):
                if isinstance(item, otio.schema.Gap):
                    gap_meta = item.metadata.get("documentary", {})
                    if gap_meta.get("scene_num") == scene_num:
                        video_track[i] = clip
                        replaced = True
                        break

        if not replaced:
            # Find correct insertion position based on (scene_num, phrase_idx).
            insert_pos = len(video_track)  # default: append at end
            for i, item in enumerate(video_track):
                meta = item.metadata.get("documentary", {})
                item_scene = meta.get("scene_num", 0)
                item_phrase = meta.get("phrase_idx", 0)
                if (item_scene, item_phrase) > (scene_num, phrase_idx):
                    insert_pos = i
                    break

            video_track.insert(insert_pos, clip)

        otio.adapters.write_to_file(timeline, timeline_path)

    logger.info(
        "Added video clip: %s (source_range=%.2fs, avail=%.2fs) [GATEKEEPER: PASS]",
        clip_name, source_range, available_range,
    )
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

    with _otio_lock:
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
    # _load_timeline now acquires _otio_lock internally, so no outer lock needed.
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
