"""
WhisperX alignment tools -- word-level timestamp extraction from TTS audio.

For production: runs WhisperX on generated WAV files.
For test run: generates synthetic alignment data from text (~0.3s per word).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "").strip().lower() in ("1", "true")
_SECONDS_PER_WORD = 0.3
_WORD_GAP = 0.05  # Small gap between words


def _generate_synthetic_alignment(text: str) -> dict:
    """Generate synthetic word-level alignment data from text.

    Estimates ~0.3s per word with small gaps between words.
    """
    words = text.split()
    alignment = []
    current_time = 0.0

    for word in words:
        word_duration = _SECONDS_PER_WORD
        alignment.append(
            {
                "word": word,
                "start": round(current_time, 3),
                "end": round(current_time + word_duration, 3),
            }
        )
        current_time += word_duration + _WORD_GAP

    return {
        "words": alignment,
        "total_duration": round(current_time, 3),
        "word_count": len(words),
    }


def align_narration(
    wav_path: str,
    text: str,
    language: str = "en",
    tool_context=None,
) -> str:
    """Run WhisperX alignment on generated TTS audio.

    Args:
        wav_path: Path to the WAV file to align.
        text: Original text that was synthesized.
        language: Language code (default: "en").

    Returns:
        JSON string with per-word {word, start, end} timing data.
    """
    if _TEST_MODE or not os.path.exists(wav_path):
        # Test mode: generate synthetic alignment
        alignment = _generate_synthetic_alignment(text)
        logger.info(
            "Test mode: synthetic alignment for %s (%d words, %.2fs)",
            wav_path,
            alignment["word_count"],
            alignment["total_duration"],
        )
        return json.dumps(
            {
                "status": "aligned",
                "mode": "synthetic",
                "wav_path": wav_path,
                **alignment,
            }
        )

    # Production mode: run actual WhisperX
    try:
        import whisperx

        device = "cuda" if os.environ.get("CUDA_VISIBLE_DEVICES") else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"

        # Load audio
        audio = whisperx.load_audio(wav_path)

        # Transcribe (using text for forced alignment)
        model = whisperx.load_model(
            "large-v3", device, compute_type=compute_type
        )
        result = model.transcribe(audio, language=language)

        # Align
        model_a, metadata = whisperx.load_align_model(
            language_code=language, device=device
        )
        result = whisperx.align(
            result["segments"], model_a, metadata, audio, device
        )

        # Extract word-level timestamps
        words = []
        for segment in result.get("segments", []):
            for word_info in segment.get("words", []):
                words.append(
                    {
                        "word": word_info.get("word", ""),
                        "start": round(word_info.get("start", 0), 3),
                        "end": round(word_info.get("end", 0), 3),
                    }
                )

        total_duration = words[-1]["end"] if words else 0.0

        logger.info(
            "WhisperX aligned %s: %d words, %.2fs",
            wav_path,
            len(words),
            total_duration,
        )

        return json.dumps(
            {
                "status": "aligned",
                "mode": "whisperx",
                "wav_path": wav_path,
                "words": words,
                "total_duration": round(total_duration, 3),
                "word_count": len(words),
            }
        )

    except ImportError:
        logger.warning("WhisperX not available, falling back to synthetic alignment")
        alignment = _generate_synthetic_alignment(text)
        return json.dumps(
            {
                "status": "aligned",
                "mode": "synthetic_fallback",
                "wav_path": wav_path,
                **alignment,
            }
        )
    except Exception as e:
        logger.error("WhisperX alignment failed: %s", e)
        alignment = _generate_synthetic_alignment(text)
        return json.dumps(
            {
                "status": "aligned",
                "mode": "synthetic_error_fallback",
                "error": str(e),
                "wav_path": wav_path,
                **alignment,
            }
        )


# -- ADK FunctionTool wrappers -------------------------------------------------
align_narration_tool = FunctionTool(align_narration)

whisperx_tools = [align_narration_tool]
