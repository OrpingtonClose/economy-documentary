"""
Timeline Guardian -- PRIMARY QA mechanism for the documentary pipeline.

This is an ``after_agent_callback`` attached to every phase agent. After
each phase completes, it loads the current OTIO timeline and validates
based on which phase just completed:

- After scenario: track structure exists (V1_Video, A1_Narration, A2_Music)
- After audio: every narration clip on A1_Narration has a valid WAV file
  AND has source_range set with duration > 0
- After visual direction: every video gap has prompt + LoRA metadata
- After production: every video clip has MP4, source_range <= available_range,
  source_range > 0, and all gaps replaced
- After assembly: final validation -- no gaps, no duplicates, audio >= video sync

ENFORCEMENT POLICY: Any validation failure raises RuntimeError immediately.
The pipeline STOPS. No silent degradation, no advisory warnings, no
"append to errors list and continue". OTIO compliance is the foremost
rule -- any deviation is factual proof the pipeline is damaged.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

# Required OTIO track names
REQUIRED_TRACKS = {"V1_Video", "A1_Narration", "A2_Music"}


def _load_timeline(state: dict):
    """Load the OTIO timeline from the pipeline state.

    Acquires the ``_otio_lock`` from :mod:`tools.otio_tools` so that reads
    are serialised against concurrent tool-call writes.  The returned
    timeline object is a *snapshot* — safe to inspect after the lock is
    released.
    """
    try:
        import opentimelineio as otio
        from tools.otio_tools import _otio_lock

        timeline_path = state.get("_timeline_path", "")
        if not timeline_path or not os.path.exists(timeline_path):
            return None
        with _otio_lock:
            return otio.adapters.read_from_file(timeline_path)
    except Exception as e:
        logger.error("Failed to load OTIO timeline: %s", e)
        return None


def _get_track(timeline, track_name: str):
    """Get a track by name from the timeline.

    Returns the track object or ``None`` if no track with that name exists.
    Note: OTIO Track objects are falsy when empty (``len(track) == 0``),
    so callers must use ``is None`` checks instead of truthiness checks.
    """
    for track in timeline.tracks:
        if track.name == track_name:
            return track
    return None


def _validate_scenario(timeline, state: dict) -> Optional[str]:
    """Validate after scenario phase: track structure exists."""
    errors = []

    track_names = {t.name for t in timeline.tracks}
    missing = REQUIRED_TRACKS - track_names
    if missing:
        errors.append(f"Missing required tracks: {missing}")

    scenes_str = state.get("scenes", "[]")
    try:
        from callbacks.deterministic_steps import extract_json_array

        scenes = extract_json_array(str(scenes_str))
        if scenes is None:
            errors.append("scenes state is not valid JSON")
        elif not scenes:
            errors.append("No scenes defined in state")
    except Exception:
        errors.append("scenes state is not valid JSON")

    return "; ".join(errors) if errors else None


def _validate_audio(timeline, state: dict) -> Optional[str]:
    """Validate after audio phase: narration clips have valid WAV files
    AND source_range is set with duration > 0."""
    errors = []

    narration_track = _get_track(timeline, "A1_Narration")
    if narration_track is None:
        return "A1_Narration track not found"

    import opentimelineio as otio

    clip_count = 0
    for item in narration_track:
        if isinstance(item, otio.schema.Clip):
            clip_count += 1

            # Check media reference exists
            media_ref = item.media_reference
            if not media_ref or not hasattr(media_ref, "target_url"):
                errors.append(
                    f"Narration clip '{item.name}' has no media reference"
                )
                continue

            # Check WAV file exists
            wav_path = media_ref.target_url
            if not wav_path or not os.path.exists(wav_path):
                errors.append(
                    f"WAV file missing for clip '{item.name}': {wav_path}"
                )

            # Check source_range is set and > 0
            if not item.source_range:
                errors.append(
                    f"Narration clip '{item.name}' has no source_range "
                    f"\u2014 timeline is damaged"
                )
            else:
                dur = item.source_range.duration.to_seconds()
                if dur <= 0:
                    errors.append(
                        f"Narration clip '{item.name}' has "
                        f"source_range duration={dur:.3f}s \u2014 must be >0"
                    )

    if clip_count == 0:
        errors.append("No narration clips found on A1_Narration")

    return "; ".join(errors) if errors else None


def _validate_visual_direction(timeline, state: dict) -> Optional[str]:
    """Validate after visual direction: video gaps have prompt + LoRA metadata."""
    errors = []

    video_track = _get_track(timeline, "V1_Video")
    if video_track is None:
        return "V1_Video track not found"

    import opentimelineio as otio

    for item in video_track:
        if isinstance(item, otio.schema.Gap):
            metadata = item.metadata.get("documentary", {})
            if not metadata.get("prompt"):
                errors.append(f"Gap '{item.name}' missing prompt metadata")
            if not metadata.get("lora_id"):
                errors.append(f"Gap '{item.name}' missing lora_id metadata")

    return "; ".join(errors) if errors else None


def _validate_production(timeline, state: dict) -> Optional[str]:
    """Validate after production: video clips have MP4, durations match,
    source_range > 0, and all scene gaps replaced."""
    errors = []

    video_track = _get_track(timeline, "V1_Video")
    if video_track is None:
        return "V1_Video track not found"

    import opentimelineio as otio

    clip_count = 0
    for item in video_track:
        if isinstance(item, otio.schema.Gap):
            gap_meta = item.metadata.get("documentary", {})
            errors.append(
                f"Scene {gap_meta.get('scene_num', '?')} video gap not "
                f"replaced with clip \u2014 production incomplete"
            )
        elif isinstance(item, otio.schema.Clip):
            clip_count += 1

            # Check MP4 file exists
            media_ref = item.media_reference
            if media_ref and hasattr(media_ref, "target_url"):
                mp4_path = media_ref.target_url
                if mp4_path and not os.path.exists(mp4_path):
                    errors.append(
                        f"MP4 file missing for clip '{item.name}': {mp4_path}"
                    )

            # Check source_range is set and > 0
            if not item.source_range:
                errors.append(
                    f"Video clip '{item.name}' has no source_range "
                    f"\u2014 timeline is damaged"
                )
            else:
                src_dur = item.source_range.duration.to_seconds()
                if src_dur <= 0:
                    errors.append(
                        f"Video clip '{item.name}' has "
                        f"source_range duration={src_dur:.3f}s \u2014 must be >0"
                    )

                # Check source_range <= available_range
                if item.available_range():
                    avail_dur = item.available_range().duration.to_seconds()
                    if src_dur > avail_dur + 0.1:  # 100ms tolerance
                        errors.append(
                            f"Clip '{item.name}' source_range ({src_dur:.2f}s) "
                            f"exceeds available_range ({avail_dur:.2f}s)"
                        )

    if clip_count == 0:
        errors.append("No video clips found on V1_Video after production")

    return "; ".join(errors) if errors else None


def _validate_assembly(timeline, state: dict) -> Optional[str]:
    """Final validation: no gaps, no duplicates, audio >= video sync."""
    errors = []

    import opentimelineio as otio

    video_track = _get_track(timeline, "V1_Video")
    narration_track = _get_track(timeline, "A1_Narration")

    if video_track is None:
        errors.append("V1_Video track not found")
    if narration_track is None:
        errors.append("A1_Narration track not found")

    if video_track is not None:
        # Check for remaining gaps
        gap_count = sum(
            1 for item in video_track if isinstance(item, otio.schema.Gap)
        )
        if gap_count > 0:
            errors.append(f"V1_Video still has {gap_count} unfilled gap(s)")

        # Check for duplicate clips (same name)
        clip_names = [
            item.name
            for item in video_track
            if isinstance(item, otio.schema.Clip)
        ]
        seen = set()
        for name in clip_names:
            if name in seen:
                errors.append(f"Duplicate video clip: '{name}'")
            seen.add(name)

    # Check audio >= video sync
    if video_track is not None and narration_track is not None:
        video_dur = video_track.trimmed_range().duration.to_seconds()
        audio_dur = narration_track.trimmed_range().duration.to_seconds()
        if video_dur < audio_dur - 0.5:  # 500ms tolerance
            errors.append(
                f"Video duration ({video_dur:.2f}s) shorter than "
                f"audio ({audio_dur:.2f}s)"
            )

    return "; ".join(errors) if errors else None


# Phase -> validator mapping
_VALIDATORS = {
    "scenario": _validate_scenario,
    "audio": _validate_audio,
    "visual_direction": _validate_visual_direction,
    "production": _validate_production,
    "assembly": _validate_assembly,
}


def timeline_guardian_callback(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """After-agent callback that validates the OTIO timeline.

    Reads ``state["pipeline_phase"]`` to determine which validation to run.

    HARD RULE: Any validation failure raises RuntimeError immediately.
    The pipeline stops -- no silent degradation.
    """
    state = callback_context.state
    phase = state.get("pipeline_phase", "")

    validator = _VALIDATORS.get(phase)
    if not validator:
        logger.debug("No timeline validation for phase: %s", phase)
        return None

    timeline = _load_timeline(state)
    if not timeline:
        if phase == "scenario":
            # Timeline may not exist yet during scenario phase
            logger.debug("No timeline yet -- skipping validation for scenario")
            return None
        error_msg = f"OTIO VIOLATION [{phase}]: timeline not found or unreadable"
        state["otio_violation"] = error_msg
        logger.error("Timeline Guardian FAIL [%s]: %s", phase, error_msg)
        raise RuntimeError(error_msg)

    error = validator(timeline, state)

    if error:
        error_msg = f"OTIO VIOLATION [{phase}]: {error}"
        state["otio_violation"] = error_msg
        logger.error("Timeline Guardian FAIL [%s]: %s", phase, error)
        raise RuntimeError(error_msg)

    # Validation passed -- clear any previous violation
    state["otio_violation"] = None
    logger.info("Timeline Guardian PASS [%s]", phase)
    return None
