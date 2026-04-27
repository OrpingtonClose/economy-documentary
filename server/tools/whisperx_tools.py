"""
WhisperX alignment tools -- word-level timestamp extraction from TTS audio.

Runs WhisperX on generated WAV files.

FAIL-LOUD POLICY (#82, SKILL.md rule 3):
    WhisperX is the duration oracle for this pipeline.  If it is
    unavailable, the alignment errors, or the WAV is missing, we raise
    RuntimeError immediately.  There is NO silent fallback to synthetic
    per-word timing -- every downstream video decision depends on real
    spoken durations.  The old silent fallback masked a 46% runtime
    shortfall (PAG run: measured 194s vs target 420s) that was only
    discovered after all 21 clips had already been generated.
"""

from __future__ import annotations

import json
import logging
import os

from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

_SECONDS_PER_WORD = 0.3
_WORD_GAP = 0.05  # Small gap between words


def _generate_synthetic_alignment(text: str) -> dict:
    """Generate synthetic word-level alignment data from text.

    NOT used as a fallback in production.  Retained for unit tests
    that need deterministic mock alignment when real WhisperX
    binaries are unavailable.

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

    FAIL LOUD: if the WAV is missing, WhisperX is not installed, or
    alignment errors, raises RuntimeError.

    Args:
        wav_path: Path to the WAV file to align.
        text: Original text that was synthesized.
        language: Language code (default: "en").

    Returns:
        JSON string with per-word {word, start, end} timing data.

    Raises:
        RuntimeError: WAV missing / WhisperX unavailable / alignment
            produced zero words.  Callers must NOT swallow this -- it is
            how the pipeline protects itself from silent duration drift
            (see #82, #86).
    """
    if not os.path.exists(wav_path):
        # Reaching this branch means the TTS worker silently dropped the
        # clip -- absolutely must fail loud.
        raise RuntimeError(
            f"WhisperX cannot align {wav_path!r}: WAV file missing. "
            f"This is a pipeline-damage signal (TTS worker failed to "
            f"persist) -- refusing to fall back to synthetic duration."
        )

    # Run actual WhisperX.  Any failure is fatal -- see module
    # docstring for rationale.
    try:
        import whisperx  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "WhisperX is not installed.  WhisperX is the authoritative "
            "duration oracle (see SKILL.md rule 3, issues #82 and #86) -- "
            "refusing to fall back to synthetic per-word timing. "
            f"Install whisperx. ({e})"
        ) from e

    try:
        device = "cuda" if os.environ.get("CUDA_VISIBLE_DEVICES") else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"

        audio = whisperx.load_audio(wav_path)

        model = whisperx.load_model(
            "large-v3", device, compute_type=compute_type
        )
        result = model.transcribe(audio, language=language)

        model_a, metadata = whisperx.load_align_model(
            language_code=language, device=device
        )
        result = whisperx.align(
            result["segments"], model_a, metadata, audio, device
        )

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

        if not words:
            raise RuntimeError(
                f"WhisperX aligned 0 words for {wav_path!r} -- the audio "
                f"is either silent or not recognisable.  Refusing to "
                f"return a 0-duration measurement."
            )

        total_duration = words[-1]["end"]

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

    except RuntimeError:
        raise
    except Exception as e:  # noqa: BLE001
        # Wrap any torch/transformers/cuda error into a loud RuntimeError
        # with the original exception chained.  See #82.
        raise RuntimeError(
            f"WhisperX alignment failed for {wav_path!r}: {e!r}. "
            f"Refusing to fall back to synthetic duration -- fix the "
            f"WhisperX worker."
        ) from e


# -- ADK FunctionTool wrappers -------------------------------------------------
align_narration_tool = FunctionTool(align_narration)
