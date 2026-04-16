"""
OTIO timeline tools -- Strands @tool wrappers for OpenTimelineIO operations.

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

from strands import tool

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


@tool(context=True)
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
        existing_path = tool_context.invocation_state.get("_timeline_path", "")
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
        tool_context.invocation_state["_timeline_path"] = path

    logger.info("Created OTIO timeline: %s (%d scenes)", path, num_scenes)

    return json.dumps(
        {
            "timeline_path": path,
            "tracks": [TRACK_V1, TRACK_A1, TRACK_A2],
            "num_scenes": num_scenes,
            "status": "created",
        }
    )


@tool
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
    state = tool_context.invocation_state if tool_context else {}
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


@tool
def add_narration_gap(
    scene_num: int,
    duration: float,
    gap_type: str,
    gap_index: int = 0,
    tool_context=None,
) -> str:
    """Add an intentional silence Gap to the A1_Narration track.

    These gaps are PLANNED pauses — breathing room between voice segments
    or transitions between scenes.  They are part of the immutable OTIO
    contract and must be rendered faithfully by the assembler.

    Args:
        scene_num: Scene number this gap belongs to (for metadata).
        duration: Duration of the silence in seconds.
        gap_type: One of "inter_voice" (pause between V1→V2→V3 within a
                  scene) or "inter_scene" (transition between scenes).
        gap_index: Unique index for this gap within the scene (for
                   idempotency — prevents duplicates on pipeline restart).

    Returns:
        JSON string with gap details.
    """
    state = tool_context.invocation_state if tool_context else {}
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

        gap_name = f"scene_{scene_num:03d}_{gap_type}_{gap_index:03d}"

        # Idempotency check: don't add duplicates on pipeline restart
        for item in narration_track:
            if isinstance(item, otio.schema.Gap) and item.name == gap_name:
                return json.dumps({
                    "status": "already_exists",
                    "gap_name": gap_name,
                    "message": "Gap already exists, skipping duplicate",
                })

        gap = otio.schema.Gap(
            name=gap_name,
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(duration * 24, 24),
            ),
        )
        gap.metadata["documentary"] = {
            "scene_num": scene_num,
            "gap_type": gap_type,
            "type": "silence",
        }
        narration_track.append(gap)
        otio.adapters.write_to_file(timeline, timeline_path)

    logger.info("Added narration gap: %s (%.2fs, type=%s)", gap_name, duration, gap_type)
    return json.dumps({
        "status": "added",
        "gap_name": gap_name,
        "duration": duration,
        "gap_type": gap_type,
    })


@tool
def add_video_gap(
    scene_num: int,
    duration: float,
    gap_type: str,
    gap_index: int = 0,
    tool_context=None,
) -> str:
    """Add an intentional visual Gap to the V1_Video track.

    These gaps correspond to planned pauses on the narration track.
    During assembly they are rendered as freeze-frames (holding the last
    frame of the preceding clip) so the viewer sees a static hold instead
    of a jarring black screen.

    The gap is inserted in sorted position by scene_num (matching the
    narration track order) rather than blindly appended, because
    add_video_clip inserts clips in sorted order too — appending gaps
    would cluster them at the end after production adds clips.

    Args:
        scene_num: Scene number this gap belongs to (for metadata).
        duration: Duration of the gap in seconds.
        gap_type: One of "inter_voice" or "inter_scene".
        gap_index: Unique index for this gap within the scene (for
                   idempotency — prevents duplicates on pipeline restart).

    Returns:
        JSON string with gap details.
    """
    state = tool_context.invocation_state if tool_context else {}
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

        gap_name = f"scene_{scene_num:03d}_{gap_type}_{gap_index:03d}"

        # Idempotency check: don't add duplicates on pipeline restart
        for item in video_track:
            if isinstance(item, otio.schema.Gap) and item.name == gap_name:
                return json.dumps({
                    "status": "already_exists",
                    "gap_name": gap_name,
                    "message": "Gap already exists, skipping duplicate",
                })

        gap = otio.schema.Gap(
            name=gap_name,
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, 24),
                duration=otio.opentime.RationalTime(duration * 24, 24),
            ),
        )
        # Assign a sort_order to gaps so they interleave correctly
        # with clips during add_video_clip's sorted insertion.
        # Inter-voice gaps use gap_index as their phrase position,
        # inter-scene gaps use a large value to sort after all phrases.
        if gap_type == "inter_scene":
            gap_sort_phrase = 9999  # after all phrases in this scene
        else:
            # inter_voice gap after voice N → sort after phrase N
            gap_sort_phrase = gap_index

        gap.metadata["documentary"] = {
            "scene_num": scene_num,
            "gap_type": gap_type,
            "type": "freeze_frame",
            "phrase_idx": gap_sort_phrase,
        }
        # Insert in sorted position by (scene_num, phrase_idx) so gaps
        # stay interleaved with clips (add_video_clip also uses this
        # sort order).
        insert_pos = len(video_track)  # default: append at end
        for i, item in enumerate(video_track):
            meta = item.metadata.get("documentary", {})
            item_scene = meta.get("scene_num", 0)
            item_phrase = meta.get("phrase_idx", 0)
            if (item_scene, item_phrase) > (scene_num, gap_sort_phrase):
                insert_pos = i
                break
        video_track.insert(insert_pos, gap)
        otio.adapters.write_to_file(timeline, timeline_path)

    logger.info("Added video gap: %s (%.2fs, type=%s)", gap_name, duration, gap_type)
    return json.dumps({
        "status": "added",
        "gap_name": gap_name,
        "duration": duration,
        "gap_type": gap_type,
    })


@tool(context=True)
def get_narration_durations_by_scene(tool_context=None) -> dict:
    """Read the OTIO timeline and return narration durations per scene.

    Returns a dict mapping scene_num -> list of (voice, duration_sec) tuples,
    ordered as they appear on the A1_Narration track.  Only includes Clip
    items — Gap items (inter-voice/inter-scene pauses) are excluded.

    This is the AUTHORITATIVE source for how long video clips must be.
    Video concepts MUST be sized to match these durations.
    """
    state = tool_context.invocation_state if tool_context else {}
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
            # Skip alternate language clips (e.g. V1_EN in dual mode)
            if sn > 0 and dur > 0 and not voice.endswith("_EN"):
                result.setdefault(sn, []).append((voice, dur))

    return result


def _gatekeeper_check_video_clip(
    timeline,
    scene_num: int,
    phrase_idx: int,
    source_range: float,
) -> Optional[str]:
    """OTIO Gatekeeper: cross-validate a video clip against narration timing.

    AUDIT-ONLY: this check logs warnings but does NOT block the OTIO write.
    The real gatekeeper validation runs as a batch AFTER all artifacts are
    uploaded to B2 (see deterministic_production_callback).  This function
    exists for early warning in the logs only.

    Checks:
    1. source_range > 0
    2. source_range <= available narration duration for this scene
    3. phrase_idx corresponds to a real narration phrase (not phantom)

    Returns None if valid, or a warning string describing the issue.
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

    # Check source_range matches corresponding narration duration (1s tolerance).
    # Account for LTX-2.3 10s cap: when narration exceeds 10s, video at 10s
    # is the best the model can do — don't reject in that case.
    _LTX_CAP = 10.0
    expected_dur = scene_narrations[phrase_idx][1]
    drift = abs(source_range - expected_dur)
    cap_explained = (
        expected_dur > _LTX_CAP
        and source_range >= _LTX_CAP - 0.5
    )
    if drift > 1.0 and not cap_explained:
        return (
            f"scene {scene_num} phrase {phrase_idx}: video source_range "
            f"({source_range:.2f}s) does not match narration duration "
            f"({expected_dur:.2f}s) — drift {drift:.2f}s > 1s. "
            f"Video clips must be sized to match narration."
        )

    return None


@tool
def add_video_clip(
    scene_num: int,
    phrase_idx: int,
    mp4_path: str,
    duration: float,
    source_range: float,
    available_range: float,
    lora_id: str,
    sub_idx: int | None = None,
    tool_context=None,
) -> str:
    """Add a video clip to V1_Video track.

    GATEKEEPER (audit-only): Before adding, cross-validates the clip's
    source_range against the narration track and logs warnings.  Does NOT
    block the write — the batch gatekeeper in the production callback
    validates after all artifacts are uploaded to B2 (audit trail).

    Idempotent: checks for existing clip with same scene_num + phrase_idx + sub_idx.

    Args:
        scene_num: Scene number (1-based).
        phrase_idx: Visual phrase index within the scene.
        mp4_path: Path to the generated MP4 file.
        duration: Target duration in seconds.
        source_range: Source range duration in seconds.
        available_range: Available range duration in seconds.
        lora_id: LoRA identifier used for generation.
        sub_idx: Optional sub-clip index for split concepts (0-based).

    Returns:
        JSON string with clip details.
    """
    state = tool_context.invocation_state if tool_context else {}
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

        # GATEKEEPER (audit-only): cross-validate against narration.
        # This does NOT block the write — the batch gatekeeper in
        # deterministic_production_callback runs after B2 upload and
        # handles rejects.  We log here for early visibility only.
        gate_warning = _gatekeeper_check_video_clip(
            timeline, scene_num, phrase_idx, source_range,
        )
        if gate_warning:
            logger.warning(
                "OTIO GATEKEEPER WARNING (audit-only): video clip scene %d phrase %d: %s",
                scene_num, phrase_idx, gate_warning,
            )

        suffix = f"_sub{sub_idx:02d}" if sub_idx is not None else ""
        clip_name = f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}{suffix}"

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
            **(({"sub_idx": sub_idx}) if sub_idx is not None else {}),
        }

        # Insert clip in correct sorted position by (scene_num, phrase_idx).
        # First, try replacing the scene's placeholder gap (only for phrase 0).
        # IMPORTANT: only replace gaps with status="empty" (placeholders from
        # create_timeline), NOT inter-voice/inter-scene structural gaps.
        replaced = False
        if phrase_idx == 0:
            for i, item in enumerate(video_track):
                if isinstance(item, otio.schema.Gap):
                    gap_meta = item.metadata.get("documentary", {})
                    if (gap_meta.get("scene_num") == scene_num
                            and gap_meta.get("status") == "empty"):
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
                item_sub = meta.get("sub_idx", -1)
                cur_sub = sub_idx if sub_idx is not None else -1
                if (item_scene, item_phrase, item_sub) > (scene_num, phrase_idx, cur_sub):
                    insert_pos = i
                    break

            video_track.insert(insert_pos, clip)

        otio.adapters.write_to_file(timeline, timeline_path)

    logger.info(
        "Added video clip: %s (source_range=%.2fs, avail=%.2fs)",
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


@tool(context=True)
def get_timeline_status(tool_context=None) -> str:
    """Get a summary of all tracks, clips, and gaps in the timeline.

    Returns:
        JSON string with timeline structure summary.
    """
    state = tool_context.invocation_state if tool_context else {}
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


@tool(context=True)
def validate_timeline(phase: str, tool_context=None) -> str:
    """Run phase-specific validation checks on the timeline.

    Args:
        phase: Pipeline phase name (scenario, audio, visual_direction,
               production, assembly).

    Returns:
        JSON string with validation results.
    """
    from callbacks.timeline_guardian import _VALIDATORS, _load_timeline

    state = tool_context.invocation_state if tool_context else {}
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


otio_tools = [
    create_timeline,
    add_narration_clip,
    add_video_clip,
    get_timeline_status,
    validate_timeline,
]
