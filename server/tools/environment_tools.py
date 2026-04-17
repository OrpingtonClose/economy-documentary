"""
Environment-probing tools for the scenario planner.

These tools let the planner discover production constraints at runtime
instead of hardcoding them in the system prompt.
"""

from __future__ import annotations

import json
import logging
import os

from strands import tool

logger = logging.getLogger(__name__)

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")


@tool
def query_production_capabilities() -> str:
    """Read config/model_manifest.json and return structured info about available models.

    Returns production capabilities including LTX-2.3 video generation limits,
    Qwen3-TTS parameters, and VRAM requirements.

    Returns:
        JSON string with production capabilities.
    """
    manifest_path = os.path.join(_CONFIG_DIR, "model_manifest.json")
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
        logger.info("Loaded model manifest from %s", manifest_path)
        return json.dumps(manifest, indent=2)
    except FileNotFoundError:
        # Return sensible defaults if manifest is missing
        defaults = {
            "video_model": {
                "name": "LTX-2.3",
                "max_clip_duration_sec": 10,
                "resolution": "1280x720",
                "fps": 24,
                "vram_gb": 48,
                "strengths": [
                    "cinematic compositions",
                    "atmospheric scenes",
                    "single-subject shots",
                    "slow camera movements",
                ],
                "weaknesses": [
                    "complex human figures",
                    "text/logos",
                    "fast action",
                    "multiple characters",
                ],
            },
            "tts_model": {
                "name": "Qwen3-TTS",
                "languages": ["en", "ru"],
                "sample_rate": 24000,
                "vram_gb": 16,
                "speech_rate_wpm": {"en": 150, "ru": 130},
            },
        }
        logger.warning(
            "Model manifest not found at %s, using defaults", manifest_path
        )
        return json.dumps(defaults, indent=2)


@tool
def estimate_tts_duration(text: str, voice: str = "V1", language: str = "en") -> str:
    """Estimate speech duration using word count heuristic calibrated to Qwen3-TTS.

    Args:
        text: The narration text to estimate duration for.
        voice: Voice role identifier (V1, V2, V3).
        language: Language code (en or ru).

    Returns:
        JSON string with estimated duration in seconds.
    """
    word_count = len(text.split())
    wpm = 130 if language == "ru" else 150
    duration_sec = (word_count / wpm) * 60.0
    duration_sec = max(1.0, duration_sec)

    return json.dumps({
        "estimated_duration_sec": round(duration_sec, 2),
        "word_count": word_count,
        "words_per_minute": wpm,
        "language": language,
        "voice": voice,
    })


@tool
def validate_plan(scenes_json: str) -> str:
    """Run gatekeeper checks against a proposed scene array.

    Args:
        scenes_json: JSON string of the proposed scenes array.

    Returns:
        JSON string with pass/fail and details.
    """
    try:
        from gatekeeper import check_scenario
        scenes = json.loads(scenes_json)
        results = check_scenario(scenes)
        has_rejects = any(
            r.verdict.value == "reject" for r in results
        )
        return json.dumps({
            "pass": not has_rejects,
            "checks": [
                {
                    "name": r.name,
                    "verdict": r.verdict.value,
                    "message": r.message,
                }
                for r in results
            ],
        })
    except ImportError:
        return json.dumps({
            "pass": True,
            "message": "Gatekeeper not available, skipping validation",
        })
    except json.JSONDecodeError as e:
        return json.dumps({
            "pass": False,
            "error": f"Invalid JSON: {e}",
        })


@tool
def query_gatekeeper_rules() -> str:
    """Return a structured description of what the gatekeeper validates.

    Returns:
        JSON string describing all gatekeeper validation rules.
    """
    rules = {
        "scenario_rules": [
            "Each scene must have exactly 3 voice blocks (V1, V2, V3)",
            "No scene exceeds maximum duration (default 45s)",
            "No rhetorical questions in narration text",
            "Valid JSON structure with required fields",
            "Visual notes present for each scene",
            "Dopamine hooks present for each scene",
        ],
        "audio_rules": [
            "Every narration clip has a valid WAV file on disk",
            "Every clip has source_range with duration > 0",
            "WhisperX alignment data is available",
        ],
        "visual_rules": [
            "Every visual concept has a valid prompt",
            "No consecutive scenes with same camera movement",
            "LoRA assignments are valid catalog entries",
            "Negative prompts derived from visual_style.avoid",
        ],
        "production_rules": [
            "Every video clip has an MP4 file on disk",
            "source_range <= available_range for all clips",
            "No placeholder gaps remaining on V1_Video",
            "Video timing matches narration duration (1s tolerance)",
        ],
        "assembly_rules": [
            "No gaps remaining on any track",
            "Audio duration >= video duration",
            "Final output file exists and is playable",
        ],
    }
    return json.dumps(rules, indent=2)


@tool
def query_voice_profiles() -> str:
    """Return available TTS voice profiles and characteristics.

    Returns:
        JSON string with voice profile information.
    """
    profiles = {
        "V1": {
            "name": "The Hook",
            "role": "Provocative opener, challenges assumptions",
            "tone": "Bold, direct, slightly confrontational",
            "speaking_rate": "Slightly faster than average",
        },
        "V2": {
            "name": "The Expert",
            "role": "Provides data, evidence, nuance",
            "tone": "Calm, authoritative, measured",
            "speaking_rate": "Moderate, clear enunciation",
        },
        "V3": {
            "name": "The Storyteller",
            "role": "Human angle, emotional connection",
            "tone": "Warm, empathetic, conversational",
            "speaking_rate": "Natural, with dramatic pauses",
        },
    }
    return json.dumps(profiles, indent=2)


@tool
def get_worker_fleet_status() -> str:
    """Return current GPU worker health summary.

    Returns:
        JSON string with worker fleet status.
    """
    try:
        from infra_agent import get_infra_agent
        infra = get_infra_agent()
        if infra:
            status = infra.get_fleet_status()
            return json.dumps(status)
        return json.dumps({"status": "no_infra_agent", "workers": []})
    except ImportError:
        return json.dumps({
            "status": "infra_agent_unavailable",
            "workers": [],
            "message": "Infrastructure agent not available",
        })


environment_tools = [
    query_production_capabilities,
    estimate_tts_duration,
    validate_plan,
    query_gatekeeper_rules,
    query_voice_profiles,
    get_worker_fleet_status,
]
