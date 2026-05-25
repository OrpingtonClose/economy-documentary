#!/usr/bin/env python3
"""Standalone Qwen3-TTS runner. Called via bash from the VM agent.

Usage:
    python run_qwen3_tts.py --text "Every rainbow is a lie..." --voice V1 --output /workspace/out.wav
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import wave

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _np_to_wav_bytes(samples: np.ndarray, sample_rate_hz: int) -> bytes:
    if samples.ndim > 1:
        samples = samples.mean(axis=tuple(range(samples.ndim - 1)))
    samples = np.asarray(samples, dtype=np.float32)
    clipped = np.clip(samples, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate_hz))
        wav.writeframes(int16.tobytes())
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen3-TTS standalone runner")
    parser.add_argument("--text", required=True, help="Text to synthesize")
    parser.add_argument("--voice", default="V1", help="Voice identifier (V1, V2, V3)")
    parser.add_argument("--output", required=True, help="Output WAV path")
    parser.add_argument("--language", default="en", help="Language code")
    args = parser.parse_args()

    # Voice → Qwen speaker mapping
    voice_map = {
        "V1": "Ryan",
        "V2": "Aiden",
        "V3": "Serena",
    }
    speaker = voice_map.get(args.voice, "Ryan")

    lang_map = {"en": "English", "ru": "Russian", "zh": "Chinese", "ja": "Japanese", "ko": "Korean"}
    language = lang_map.get(args.language, "Auto")

    logger.info("Loading Qwen3-TTS model...")
    import torch
    from qwen_tts import Qwen3TTSModel

    models_dir = "/workspace/models"
    model_path = os.path.join(models_dir, "qwen3-tts-voicedesign")

    model = Qwen3TTSModel.from_pretrained(
        model_path,
        device_map="cuda:0",
        dtype=torch.bfloat16,
    )

    logger.info("Synthesizing: voice=%s speaker=%s lang=%s chars=%d", args.voice, speaker, language, len(args.text))
    wavs, sr = model.generate_custom_voice(
        text=args.text,
        language=language,
        speaker=speaker,
    )

    wav = np.asarray(wavs[0])
    sample_rate = int(sr) if sr else 24000
    wav_bytes = _np_to_wav_bytes(wav, sample_rate)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "wb") as f:
        f.write(wav_bytes)

    duration_s = len(wav) / sample_rate
    logger.info("Done: %s (%.1fs, %d bytes)", args.output, duration_s, len(wav_bytes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
