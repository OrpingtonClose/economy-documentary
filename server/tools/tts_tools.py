"""
TTS tools -- Qwen3-TTS generation wrapper.

For production: generates narration WAV files using Qwen3-TTS on GPU.
For test run: generates silent WAV files with correct estimated duration (no GPU).
"""

from __future__ import annotations

import json
import logging
import os
import struct
import wave
from typing import Optional

from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

_OUTPUT_BASE = os.environ.get(
    "TTS_OUTPUT_DIR", "/tmp/documentary-pipeline/audio"
)
_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "").strip() == "1"

# Approximate speech rate: ~0.3s per word (for test duration estimation)
_SECONDS_PER_WORD = 0.3
_SAMPLE_RATE = 24000


def _estimate_duration(text: str) -> float:
    """Estimate speech duration from text length."""
    word_count = len(text.split())
    return max(1.0, word_count * _SECONDS_PER_WORD)


def _generate_silent_wav(output_path: str, duration: float) -> None:
    """Generate a silent WAV file with the specified duration."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    num_frames = int(_SAMPLE_RATE * duration)
    with wave.open(output_path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        # Write silence (zeros)
        silent_data = struct.pack("<" + "h" * num_frames, *([0] * num_frames))
        wf.writeframes(silent_data)


def generate_narration(
    scene_num: int,
    voice_role: str,
    text: str,
    output_dir: str = "",
    tool_context=None,
) -> str:
    """Generate narration WAV file using Qwen3-TTS.

    Args:
        scene_num: Scene number (1-based).
        voice_role: Voice role identifier (e.g., "V1", "V2", "V3").
        text: Narration text to synthesize.
        output_dir: Optional output directory override.

    Returns:
        JSON string with WAV path and duration.
    """
    out_dir = output_dir or _OUTPUT_BASE
    os.makedirs(out_dir, exist_ok=True)

    filename = f"scene_{scene_num:03d}_{voice_role}.wav"
    wav_path = os.path.join(out_dir, filename)

    duration = _estimate_duration(text)

    if _TEST_MODE:
        # Test mode: generate silent WAV with correct duration
        _generate_silent_wav(wav_path, duration)
        logger.info(
            "Test mode: generated silent WAV %s (%.2fs)", wav_path, duration
        )
        return json.dumps(
            {
                "status": "generated",
                "mode": "test",
                "wav_path": wav_path,
                "duration": round(duration, 2),
                "sample_rate": _SAMPLE_RATE,
                "text_length": len(text),
                "word_count": len(text.split()),
            }
        )

    # Production mode: call Qwen3-TTS
    # TODO: Implement actual Qwen3-TTS call on GPU VM
    # For now, generate silent WAV as placeholder
    _generate_silent_wav(wav_path, duration)
    logger.info(
        "Generated narration WAV %s (%.2fs, %d words)",
        wav_path,
        duration,
        len(text.split()),
    )

    return json.dumps(
        {
            "status": "generated",
            "mode": "placeholder",
            "wav_path": wav_path,
            "duration": round(duration, 2),
            "sample_rate": _SAMPLE_RATE,
            "text_length": len(text),
            "word_count": len(text.split()),
        }
    )


# -- ADK FunctionTool wrappers -------------------------------------------------
generate_narration_tool = FunctionTool(generate_narration)

tts_tools = [generate_narration_tool]
