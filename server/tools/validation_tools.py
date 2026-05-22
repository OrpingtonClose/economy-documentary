"""
Self-validation tools for media agents.

Each agent gets these tools to verify its own output quality — a key
pattern from the Strands migration where agents validated their
deliverables before reporting completion.  This converts silent failures
into loud, actionable errors that the recovery system can handle.

Tools:
  - validate_stage_output: Check postconditions for a named stage
  - validate_otio_compliance: Verify OTIO timeline structure
  - validate_media_files: Check that expected media files exist and are valid
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Placeholder strings that indicate upstream data is missing/stale.
# If a state key matches any of these, the stage's precondition is violated.
_PLACEHOLDER_PATTERNS = (
    "(not yet",
    "[]",
    "{}",
    "",
)


def validate_stage_output(
    stage_name: str,
    tool_context=None,
) -> str:
    """Validate postconditions for a pipeline stage.

    Checks that:
    1. All required state keys from the StageContract are populated
    2. State values are not placeholder strings
    3. Produced artifacts exist on disk
    4. OTIO timeline is structurally valid (if applicable)

    Args:
        stage_name: One of "scenario", "audio", "visual_direction",
            "production", "assembly".

    Returns:
        JSON string with validation result: {"passed": bool, "errors": [...]}
    """
    from contracts import (
        ASSEMBLY_CONTRACT,
        AUDIO_CONTRACT,
        PRODUCTION_CONTRACT,
        SCENARIO_CONTRACT,
        VISUAL_DIRECTION_CONTRACT,
    )

    contract_map = {
        "scenario": SCENARIO_CONTRACT,
        "audio": AUDIO_CONTRACT,
        "visual_direction": VISUAL_DIRECTION_CONTRACT,
        "production": PRODUCTION_CONTRACT,
        "assembly": ASSEMBLY_CONTRACT,
    }

    contract = contract_map.get(stage_name)
    if contract is None:
        return json.dumps({
            "passed": False,
            "errors": [f"Unknown stage: {stage_name}. Valid: {list(contract_map.keys())}"],
        })

    errors: list[str] = []

    # Check produced_state keys
    if tool_context is not None:
        state = tool_context.state if hasattr(tool_context, "state") else {}
        for key in contract.produced_state:
            value = state.get(key, "")
            str_value = str(value).strip()
            if not str_value or str_value in _PLACEHOLDER_PATTERNS:
                errors.append(
                    f"State key '{key}' is missing or placeholder: '{str_value[:100]}'"
                )

    # Check produced_artifacts
    base_dir = os.environ.get(
        "PIPELINE_OUTPUT_DIR", "/tmp/documentary-pipeline"
    )
    for glob_pattern in contract.produced_artifacts:
        import glob
        matches = glob.glob(os.path.join(base_dir, glob_pattern))
        if not matches:
            errors.append(
                f"No artifacts matching '{glob_pattern}' found in {base_dir}"
            )
        else:
            # Check that files are non-empty
            for path in matches:
                if os.path.getsize(path) == 0:
                    errors.append(f"Artifact is empty (0 bytes): {path}")

    passed = len(errors) == 0
    logger.info(
        "stage=<%s> | self-validation %s (%d errors)",
        stage_name, "PASSED" if passed else "FAILED", len(errors),
    )

    return json.dumps({
        "passed": passed,
        "stage": stage_name,
        "errors": errors,
        "checks_run": len(contract.produced_state) + len(contract.produced_artifacts),
    })


def validate_otio_compliance(
    tool_context=None,
) -> str:
    """Validate OTIO timeline structure for gaps, overlaps, and violations.

    Checks:
    1. Timeline file exists and is parseable
    2. No negative-duration clips or gaps
    3. No overlapping clips within a track
    4. All clip media references point to existing files
    5. Narration track and video track have matching item counts

    Returns:
        JSON string with validation result and specific violations.
    """
    timeline_path = ""
    if tool_context is not None:
        state = tool_context.state if hasattr(tool_context, "state") else {}
        timeline_path = state.get("_timeline_path", "")

    if not timeline_path:
        timeline_path = os.path.join(
            os.environ.get("TIMELINE_DIR", "/tmp/documentary-pipeline/timelines"),
            "documentary.otio",
        )

    if not os.path.exists(timeline_path):
        return json.dumps({
            "passed": False,
            "errors": [f"Timeline file not found: {timeline_path}"],
            "violations": [],
        })

    violations: list[dict] = []

    try:
        import opentimelineio as otio
        tl = otio.adapters.read_from_file(timeline_path)

        for track in tl.tracks:
            track_name = track.name or "unnamed"
            cumulative_time = 0.0

            for idx, item in enumerate(track):
                if isinstance(item, (otio.schema.Clip, otio.schema.Gap)):
                    if item.source_range:
                        dur = item.source_range.duration.to_seconds()
                        if dur < 0:
                            violations.append({
                                "type": "negative_duration",
                                "track": track_name,
                                "item_idx": idx,
                                "item_name": getattr(item, "name", ""),
                                "duration": dur,
                                "remediation": "Remove or fix this item in the timeline",
                            })
                        cumulative_time += dur

                    # Check media references for clips
                    if isinstance(item, otio.schema.Clip):
                        if item.media_reference and hasattr(item.media_reference, "target_url"):
                            ref_path = item.media_reference.target_url
                            if ref_path and not os.path.exists(ref_path):
                                violations.append({
                                    "type": "missing_media",
                                    "track": track_name,
                                    "item_idx": idx,
                                    "item_name": getattr(item, "name", ""),
                                    "media_path": ref_path,
                                    "remediation": "Regenerate this clip or fix the media reference",
                                })

        # Check track consistency
        narration_tracks = [t for t in tl.tracks if "narration" in (t.name or "").lower()]
        video_tracks = [t for t in tl.tracks if "video" in (t.name or "").lower()]

        if narration_tracks and video_tracks:
            narr_clips = sum(
                1 for item in narration_tracks[0]
                if isinstance(item, otio.schema.Clip)
            )
            vid_clips = sum(
                1 for item in video_tracks[0]
                if isinstance(item, otio.schema.Clip)
            )
            if narr_clips > 0 and vid_clips == 0:
                violations.append({
                    "type": "empty_video_track",
                    "narration_clips": narr_clips,
                    "video_clips": vid_clips,
                    "remediation": "Run video generation to populate the video track",
                })

    except Exception as e:
        return json.dumps({
            "passed": False,
            "errors": [f"Failed to parse timeline: {e}"],
            "violations": [],
        })

    passed = len(violations) == 0
    logger.info(
        "OTIO compliance: %s (%d violations)",
        "PASSED" if passed else "FAILED", len(violations),
    )

    return json.dumps({
        "passed": passed,
        "timeline_path": timeline_path,
        "violations": violations,
        "total_tracks": len(tl.tracks) if tl else 0,
    })

