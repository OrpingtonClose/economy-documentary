"""
TTS tools -- Qwen3-TTS generation wrapper.

For production: generates narration WAV files using Qwen3-TTS on GPU.
For test run: generates silent WAV files with correct estimated duration (no GPU).
"""

from __future__ import annotations

import json
import logging
import os
import wave
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

_OUTPUT_BASE = os.environ.get(
    "TTS_OUTPUT_DIR", "/tmp/documentary-pipeline/audio"
)
_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "").strip().lower() in ("1", "true")

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
        # Write silence — 16-bit PCM silence is simply zero bytes
        silent_data = b'\x00' * (num_frames * 2)
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

    # Production mode: call Qwen3-TTS on GPU worker
    gpu_worker_url = os.environ.get("GPU_WORKER_URL", "")
    if not gpu_worker_url:
        # Fallback: generate silent WAV if no GPU worker configured
        logger.warning("GPU_WORKER_URL not set, generating silent WAV placeholder")
        _generate_silent_wav(wav_path, duration)
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

    # Determine language from voice_role suffix (e.g., "V1_RU" -> "ru")
    lang = "en"
    voice = voice_role
    if voice_role.endswith("_RU"):
        lang = "ru"
        voice = voice_role[:-3]  # Strip _RU suffix
    elif voice_role.endswith("_EN"):
        lang = "en"
        voice = voice_role[:-3]  # Strip _EN suffix

    payload = json.dumps({
        "text": text,
        "voice": voice,
        "language": lang,
        "scene_num": scene_num,
        "sample_rate": _SAMPLE_RATE,
    }).encode("utf-8")

    tts_url = f"{gpu_worker_url.rstrip('/')}/tts"
    req = Request(tts_url, data=payload, headers={"Content-Type": "application/json"})

    try:
        with urlopen(req, timeout=120) as resp:
            wav_bytes = resp.read()
            actual_duration = float(resp.headers.get("X-Audio-Duration", str(duration)))
            gen_time = float(resp.headers.get("X-Gen-Time", "0"))

        os.makedirs(os.path.dirname(wav_path) or ".", exist_ok=True)
        with open(wav_path, "wb") as f:
            f.write(wav_bytes)

        logger.info(
            "Generated narration WAV %s (%.2fs, gen=%.1fs, %d words)",
            wav_path, actual_duration, gen_time, len(text.split()),
        )
        return json.dumps(
            {
                "status": "generated",
                "mode": "production",
                "wav_path": wav_path,
                "duration": round(actual_duration, 2),
                "sample_rate": _SAMPLE_RATE,
                "text_length": len(text),
                "word_count": len(text.split()),
                "gen_time": round(gen_time, 2),
            }
        )
    except (URLError, OSError, TimeoutError) as exc:
        logger.error("GPU worker TTS request failed: %s", exc)
        # Fallback to silent WAV so pipeline can continue
        _generate_silent_wav(wav_path, duration)
        return json.dumps(
            {
                "status": "generated",
                "mode": "fallback",
                "wav_path": wav_path,
                "duration": round(duration, 2),
                "sample_rate": _SAMPLE_RATE,
                "error": str(exc),
            }
        )


# -- ADK FunctionTool wrappers -------------------------------------------------
generate_narration_tool = FunctionTool(generate_narration)

tts_tools = [generate_narration_tool]
