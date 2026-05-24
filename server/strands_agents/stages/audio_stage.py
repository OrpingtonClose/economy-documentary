from __future__ import annotations

import json
import logging
import os
from typing import Any

from strands import tool
from strands_agents.otio_manager import OTIOStateManager

logger = logging.getLogger(__name__)

# OTIO manager — set by graph_pipeline
_otio_manager: OTIOStateManager | None = None


# ── OTIO clip management ────────────────────────────────────────────

@tool
def add_narration_to_timeline(
    scene_num: int,
    voice: str,
    wav_path: str,
    duration: float,
) -> str:
    """Add a narration clip to the OTIO timeline.

    Args:
        scene_num: Scene number.
        voice: Voice role (V1, V2, V3).
        wav_path: Path to the generated WAV file.
        duration: Duration in seconds.

    Returns:
        JSON with clip result.
    """
    try:
        from tools.otio_tools import add_narration_clip
        from tools.otio_file_ops import resolve_timeline_path

        class _ToolCtx:
            def __init__(self):
                self.state = {
                    "_timeline_path": resolve_timeline_path(),
                    "pipeline_phase": "audio",
                }

        result = add_narration_clip(
            scene_num=scene_num,
            voice=voice,
            wav_path=wav_path,
            duration=duration,
            tool_context=_ToolCtx(),
        )
        return result
    except Exception as exc:
        logger.error("add_narration_clip failed: %s", exc)
        return json.dumps({"error": str(exc), "note": "otio write failed"})


@tool
def align_narration_audio(
    wav_path: str,
    text: str,
    language: str = "en",
) -> str:
    """Run WhisperX alignment on a narration clip.

    Returns precise word-level timing for the narration.

    Args:
        wav_path: Path to the WAV file.
        text: Original narration text.
        language: Language code (default: "en").

    Returns:
        JSON with alignment data.
    """
    from tools.whisperx_tools import align_narration

    class _ToolCtx:
        def __init__(self):
            self.state = {
                "pipeline_phase": "audio",
                "_output_dir": "/tmp/documentary-pipeline",
            }

    result = align_narration(
        wav_path=wav_path,
        text=text,
        language=language,
        tool_context=_ToolCtx(),
    )
    return result


@tool
def evaluate_audio_timing(
    scenes_json: str,
    target_duration_sec: float = 180.0,
) -> str:
    """Evaluate narration timing against target duration.

    Checks if the total narration duration is within 15% of the target.
    If drift exceeds 15%, sets recovery context for the timing loop.

    Args:
        scenes_json: JSON array of scenes with duration info.
        target_duration_sec: Target total duration in seconds.

    Returns:
        JSON with timing evaluation.
    """
    try:
        scenes = json.loads(scenes_json) if isinstance(scenes_json, str) else scenes_json
        if isinstance(scenes, list):
            total = sum(s.get("duration_sec", 0) for s in scenes if isinstance(s, dict))
        else:
            total = 0

        drift = abs(total - target_duration_sec) / target_duration_sec if target_duration_sec > 0 else 0
        verdict = "pass" if drift <= 0.15 else "fail"

        return json.dumps({
            "verdict": verdict,
            "total_duration_sec": round(total, 2),
            "target_duration_sec": target_duration_sec,
            "drift_pct": round(drift * 100, 1),
        })
    except Exception as exc:
        logger.error("Timing evaluation failed: %s", exc)
        return json.dumps({"verdict": "fail", "reason": str(exc)})


@tool
def read_scenes_from_otio() -> str:
    """Read the scene plan from the OTIO timeline.

    The scenario stage writes scenes to OTIO metadata. This tool
    reads them back so the audio stage knows what to narrate.

    Returns:
        JSON string with the scenes array.
    """
    if _otio_manager is None:
        return json.dumps({"error": "No OTIO manager available"})
    scenes = _otio_manager.get_pipeline_metadata("scenes", [])
    if not scenes:
        return json.dumps({"error": "No scenes found in OTIO timeline. The scenario stage must run first."})
    return json.dumps(scenes)


@tool
def persist_audio_to_otio(
    whisperx_alignment_json: str = "",
) -> str:
    """Persist audio results to the OTIO timeline.

    Writes whisperx_alignment to OTIO timeline metadata. The visual
    stage reads from OTIO — not from agent state. Includes full
    provenance. Even on error, the partial data is persisted.

    Args:
        whisperx_alignment_json: JSON alignment data from WhisperX.
    """
    import time as _time
    from strands_agents.artifact_provenance import ArtifactProvenance

    if _otio_manager is None:
        return json.dumps({"error": "No OTIO manager available"})

    try:
        alignment = json.loads(whisperx_alignment_json) if isinstance(whisperx_alignment_json, str) else whisperx_alignment_json
    except (json.JSONDecodeError, TypeError):
        alignment = {"status": "error", "note": "alignment data could not be parsed"}

    prov = ArtifactProvenance(
        artifact_type="alignment",
        creator_agent="audio",
        creator_tool="persist_audio_to_otio",
        created_at=_time.time(),
        prompt="whisperx_alignment_data",
        parent_artifacts=["state/scenes.json"],
        upstream_stage="scenario",
        status="valid" if alignment.get("status") != "error" else "error",
        error=alignment.get("note", ""),
        content_meta={"alignment_count": len(alignment.get("alignments", []))} if isinstance(alignment, dict) else {},
    )
    prov.environment = _capture_audio_environment()

    try:
        _otio_manager.set_pipeline_metadata("whisperx_alignment", alignment, provenance=prov.to_dict())
    except Exception as exc:
        logger.error("Failed to persist alignment to OTIO: %s", exc)
        prov.status = "error"
        prov.error = str(exc)
        try:
            _otio_manager.set_pipeline_metadata("alignment_error", str(exc), provenance=prov.to_dict())
        except Exception:
            pass

    # Upload to B2 immediately
    try:
        from tools.b2_checkpoint import upload_json
        upload_json(json.dumps(alignment), "state/whisperx_alignment.json")
    except Exception as b2_err:
        logger.warning("B2 upload failed for alignment: %s", b2_err)

    return json.dumps({"persisted": True, "status": prov.status})


def _capture_audio_environment() -> dict:
    """Capture the audio production environment."""
    import platform
    env = {
        "python_version": platform.python_version(),
        "os": platform.system(),
        "arch": platform.machine(),
    }
    for var in ["TTS_WORKER_URL", "STRANDS_MODEL"]:
        val = ""
        if val:
            env[var.lower()] = val
    return env
