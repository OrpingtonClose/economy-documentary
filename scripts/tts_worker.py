#!/usr/bin/env python3
"""Qwen3-TTS Worker — plain text GET/POST interface.

Loads Qwen3-TTS model on startup.
GET / → plain text status
POST / → raw text narration, returns WAV bytes

Run:
    python tts_worker.py --port 8880
"""
import argparse
import io
import logging
import os
import wave

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("tts_worker")

app = FastAPI()

# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

_tts_model = None


def _load_tts():
    """Lazy-load Qwen3-TTS model."""
    global _tts_model
    if _tts_model is not None:
        return _tts_model

    try:
        from qwen_tts import Qwen3TTSModel

        model_path = "/workspace/models/qwen3-tts-voicedesign"
        logger.info("Loading Qwen3-TTS from %s ...", model_path)
        _tts_model = Qwen3TTSModel.from_pretrained(model_path)
        logger.info("Qwen3-TTS loaded.")
        return _tts_model
    except Exception as exc:
        logger.error("Failed to load Qwen3-TTS: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def get_endpoint():
    if _tts_model is None:
        return Response(content="loading model...", media_type="text/plain", status_code=503)
    import torch
    if torch.cuda.is_available():
        gpu = torch.cuda.get_device_name(0)
        vram_total = torch.cuda.get_device_properties(0).total_memory / 1e9
        vram_used = torch.cuda.memory_allocated(0) / 1e9
        return Response(
            content=f"ok {gpu} vram={vram_used:.1f}/{vram_total:.1f}GB mode=tts",
            media_type="text/plain",
        )
    return Response(content="ok CPU mode=tts", media_type="text/plain")


@app.post("/")
async def post_endpoint(request: Request):
    text = (await request.body()).decode("utf-8").strip()
    if not text:
        return Response(content="(no text)", media_type="text/plain", status_code=400)

    try:
        model = _load_tts()
        import numpy as np

        logger.info("Generating TTS for text: %s...", text[:60])

        # Use default voice/speaker
        wavs, sr = model.generate_custom_voice(
            text=text,
            language="en",
            speaker="default",
        )
        if not isinstance(wavs, (list, tuple)) or len(wavs) == 0:
            raise RuntimeError("qwen3-tts returned empty waveform list")

        wav = np.asarray(wavs[0])
        sample_rate = int(sr) if sr else 24000
        num_samples = int(np.asarray(wav).shape[-1])
        duration_sec = float(num_samples) / float(sample_rate)

        # Convert to int16 WAV
        if wav.dtype != np.int16:
            wav = np.clip(wav * 32767, -32768, 32767).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(wav.tobytes())

        wav_bytes = buf.getvalue()
        logger.info("Generated %d bytes WAV (%.1fs)", len(wav_bytes), duration_sec)

        return Response(
            content=wav_bytes,
            media_type="audio/wav",
            headers={
                "X-Audio-Duration": f"{duration_sec:.3f}",
                "X-Sample-Rate": str(sample_rate),
            },
        )
    except Exception as exc:
        logger.error("TTS generation failed: %s", exc)
        return Response(content=str(exc), media_type="text/plain", status_code=500)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
