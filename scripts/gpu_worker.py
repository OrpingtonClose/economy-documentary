#!/usr/bin/env python3
"""GPU Worker — FastAPI service for TTS and video generation.

Runs on a Vast.ai GPU VM. Exposes HTTP endpoints that the pipeline
calls to generate narration (Qwen3-TTS) and video clips (LTX-2.3).

Usage:
    python gpu_worker.py [--port 8880] [--models-dir /workspace/models]

Models are expected at:
    {models_dir}/qwen3-tts/     — Qwen3-TTS-12Hz-1.7B-Base
    {models_dir}/ltx2/          — LTX-2.3 diffusers-format components
                                  (model_index.json, text_encoder/, transformer/,
                                   vae/, audio_vae/, vocoder/, connectors/, etc.)

The bootstrap script (gpu_bootstrap.sh) downloads these from B2.
Requires diffusers >= 0.37.0 for LTX2Pipeline support.
bf16 only — no FP8, no quantization.
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import time
import uuid

import numpy as np
import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("gpu_worker")

app = FastAPI(title="Documentary GPU Worker")

# ---------------------------------------------------------------------------
# Global model handles (loaded once at startup)
# ---------------------------------------------------------------------------
_tts_model = None
_tts_tokenizer = None
_tts_processor = None
_ltx_pipe = None
_models_dir: str = "/workspace/models"
_output_dir: str = "/workspace/output"

# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------

class TTSRequest(BaseModel):
    text: str
    voice: str = "V1"  # V1/V2/V3 — mapped to speaker profiles
    language: str = "en"  # "en" or "ru"
    scene_num: int = 1
    sample_rate: int = 24000


class VideoRequest(BaseModel):
    prompt: str
    duration_sec: float = 5.0
    width: int = 768
    height: int = 512
    num_frames: int | None = None  # auto-calculated from duration if None
    seed: int = 42
    num_inference_steps: int = 8  # distilled model uses 8 steps
    guidance_scale: float = 1.0  # distilled model uses CFG=1


class HealthResponse(BaseModel):
    status: str
    gpu: str
    tts_loaded: bool
    ltx_loaded: bool
    vram_used_gb: float
    vram_total_gb: float


# ---------------------------------------------------------------------------
# Voice profiles for 3-voice narration
# ---------------------------------------------------------------------------
_VOICE_PROFILES = {
    "V1": {
        "en": "You are a warm, authoritative narrator with a deep voice.",
        "ru": "Вы тёплый, авторитетный рассказчик с глубоким голосом.",
    },
    "V2": {
        "en": "You are an enthusiastic, energetic presenter with a bright voice.",
        "ru": "Вы энергичный, увлечённый ведущий с ярким голосом.",
    },
    "V3": {
        "en": "You are a thoughtful, calm commentator with a measured voice.",
        "ru": "Вы вдумчивый, спокойный комментатор с размеренным голосом.",
    },
}


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def _load_tts():
    """Load Qwen3-TTS model."""
    global _tts_model, _tts_tokenizer, _tts_processor
    if _tts_model is not None:
        return

    from transformers import AutoModelForCausalLM, AutoTokenizer, AutoProcessor

    model_path = os.path.join(_models_dir, "qwen3-tts")
    logger.info("Loading Qwen3-TTS from %s ...", model_path)
    t0 = time.time()

    _tts_tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    _tts_processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)
    _tts_model = AutoModelForCausalLM.from_pretrained(
        model_path,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )

    logger.info("Qwen3-TTS loaded in %.1fs", time.time() - t0)


def _load_ltx():
    """Load LTX-2.3 pipeline via diffusers (>= 0.37.0).

    Uses enable_model_cpu_offload() to fit the full pipeline
    (Gemma3 text encoder + LTX2 transformer + VAE) on 24 GB VRAM
    by keeping idle components in CPU RAM.
    """
    global _ltx_pipe
    if _ltx_pipe is not None:
        return

    from diffusers import LTX2Pipeline

    model_path = os.path.join(_models_dir, "ltx2")
    logger.info("Loading LTX-2.3 via diffusers from %s ...", model_path)
    t0 = time.time()

    if not os.path.isfile(os.path.join(model_path, "model_index.json")):
        raise FileNotFoundError(f"model_index.json not found in {model_path}")

    _ltx_pipe = LTX2Pipeline.from_pretrained(
        model_path,
        torch_dtype=torch.bfloat16,
    )
    # CPU offload: only the active component sits on GPU at any time
    _ltx_pipe.enable_model_cpu_offload()

    logger.info("LTX-2.3 loaded in %.1fs", time.time() - t0)


# ---------------------------------------------------------------------------
# TTS generation
# ---------------------------------------------------------------------------

def _generate_tts(text: str, voice: str, language: str) -> tuple[np.ndarray, int]:
    """Generate speech audio using Qwen3-TTS.

    Returns (audio_array, sample_rate).
    """
    _load_tts()

    profile = _VOICE_PROFILES.get(voice, _VOICE_PROFILES["V1"])
    voice_instruction = profile.get(language, profile.get("en", _VOICE_PROFILES["V1"]["en"]))

    # Build the chat-style prompt for Qwen3-TTS
    messages = [
        {"role": "system", "content": voice_instruction},
        {"role": "user", "content": text},
    ]

    # Tokenize
    inputs = _tts_tokenizer.apply_chat_template(
        messages,
        return_tensors="pt",
        add_generation_prompt=True,
        tokenize=True,
    )
    if isinstance(inputs, torch.Tensor):
        input_ids = inputs.to(_tts_model.device)
    else:
        input_ids = inputs["input_ids"].to(_tts_model.device)

    # Generate speech tokens
    with torch.no_grad():
        outputs = _tts_model.generate(
            input_ids,
            max_new_tokens=4096,
            do_sample=True,
            temperature=0.7,
            top_p=0.9,
        )

    # Decode speech tokens to audio using the processor/tokenizer
    # Extract only the new tokens (after the input)
    new_tokens = outputs[0][input_ids.shape[-1]:]

    # Use the processor to convert tokens to audio
    audio = _tts_processor.decode(new_tokens, skip_special_tokens=True)

    if isinstance(audio, dict) and "audio" in audio:
        audio_array = audio["audio"]
        sr = audio.get("sampling_rate", 24000)
    elif isinstance(audio, np.ndarray):
        audio_array = audio
        sr = 24000
    else:
        # Fallback: the output might be in a different format depending
        # on the exact Qwen3-TTS version. Try to extract audio.
        audio_array = np.array(audio, dtype=np.float32)
        sr = 24000

    return audio_array, sr


# ---------------------------------------------------------------------------
# Video generation (LTX-2.3 via diffusers LTX2Pipeline)
# ---------------------------------------------------------------------------

def _generate_video(
    prompt: str,
    duration_sec: float,
    width: int,
    height: int,
    num_frames: int | None,
    seed: int,
    num_inference_steps: int,
    guidance_scale: float,
) -> bytes:
    """Generate video clip using LTX-2.3 distilled via diffusers.

    Returns raw MP4 bytes.
    """
    _load_ltx()

    from diffusers.utils import export_to_video

    fps = 24
    if num_frames is None:
        # LTX-2.3 works in chunks of 8+1 frames
        raw_frames = int(duration_sec * fps)
        # Round to nearest valid frame count: 8k+1
        num_frames = max(9, ((raw_frames - 1) // 8) * 8 + 1)

    negative_prompt = (
        "worst quality, inconsistent motion, blurry, jittery, distorted, "
        "static, low resolution"
    )

    generator = torch.Generator("cpu").manual_seed(seed)

    logger.info(
        "Generating video: %d frames (%.1fs), %dx%d, seed=%d",
        num_frames, num_frames / fps, width, height, seed,
    )
    t0 = time.time()

    result = _ltx_pipe(
        prompt=prompt,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        num_frames=num_frames,
        num_inference_steps=num_inference_steps,
        guidance_scale=guidance_scale,
        generator=generator,
    )

    elapsed = time.time() - t0
    logger.info("Video generated in %.1fs", elapsed)

    # Export to MP4
    output_path = os.path.join(
        _output_dir, f"clip_{uuid.uuid4().hex[:8]}.mp4"
    )
    os.makedirs(_output_dir, exist_ok=True)

    video_frames = result.frames[0]
    export_to_video(video_frames, output_path, fps=fps)

    with open(output_path, "rb") as f:
        data = f.read()

    try:
        os.unlink(output_path)
    except OSError:
        pass

    return data


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> HealthResponse:
    gpu_name = "unknown"
    vram_used = 0.0
    vram_total = 0.0
    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)
        vram_used = torch.cuda.memory_allocated(0) / 1e9
        vram_total = torch.cuda.get_device_properties(0).total_memory / 1e9

    return HealthResponse(
        status="ok",
        gpu=gpu_name,
        tts_loaded=_tts_model is not None,
        ltx_loaded=_ltx_pipe is not None,
        vram_used_gb=round(vram_used, 2),
        vram_total_gb=round(vram_total, 2),
    )


@app.post("/tts")
def tts_endpoint(req: TTSRequest):
    """Generate narration audio. Returns WAV bytes."""
    logger.info(
        "TTS request: scene=%d voice=%s lang=%s text=%d chars",
        req.scene_num, req.voice, req.language, len(req.text),
    )
    t0 = time.time()

    try:
        audio_array, sr = _generate_tts(req.text, req.voice, req.language)
    except Exception as e:
        logger.error("TTS failed: %s", e, exc_info=True)
        raise HTTPException(500, f"TTS generation failed: {e}")

    # Encode as WAV
    buf = io.BytesIO()
    sf.write(buf, audio_array, sr, format="WAV", subtype="PCM_16")
    wav_bytes = buf.getvalue()

    elapsed = time.time() - t0
    duration = len(audio_array) / sr
    logger.info(
        "TTS done: %.1fs audio in %.1fs (%.1fx realtime)",
        duration, elapsed, duration / max(elapsed, 0.01),
    )

    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={
            "X-Audio-Duration": str(round(duration, 3)),
            "X-Sample-Rate": str(sr),
            "X-Gen-Time": str(round(elapsed, 3)),
        },
    )


@app.post("/video")
def video_endpoint(req: VideoRequest):
    """Generate video clip. Returns MP4 bytes."""
    logger.info(
        "Video request: %.1fs, %dx%d, seed=%d, prompt=%.100s...",
        req.duration_sec, req.width, req.height, req.seed, req.prompt,
    )
    t0 = time.time()

    try:
        mp4_bytes = _generate_video(
            prompt=req.prompt,
            duration_sec=req.duration_sec,
            width=req.width,
            height=req.height,
            num_frames=req.num_frames,
            seed=req.seed,
            num_inference_steps=req.num_inference_steps,
            guidance_scale=req.guidance_scale,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Video failed: %s", e, exc_info=True)
        raise HTTPException(500, f"Video generation failed: {e}")

    elapsed = time.time() - t0
    logger.info("Video done: %d bytes in %.1fs", len(mp4_bytes), elapsed)

    return Response(
        content=mp4_bytes,
        media_type="video/mp4",
        headers={
            "X-Gen-Time": str(round(elapsed, 3)),
            "X-File-Size": str(len(mp4_bytes)),
        },
    )


@app.post("/load-models")
def load_models():
    """Pre-load all models into VRAM."""
    results = {}

    try:
        _load_tts()
        results["tts"] = "loaded"
    except Exception as e:
        results["tts"] = f"error: {e}"

    try:
        _load_ltx()
        results["ltx"] = "loaded"
    except Exception as e:
        results["ltx"] = f"error: {e}"

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GPU Worker for Documentary Pipeline")
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--models-dir", type=str, default="/workspace/models")
    parser.add_argument("--output-dir", type=str, default="/workspace/output")
    args = parser.parse_args()

    global _models_dir, _output_dir
    _models_dir = args.models_dir
    _output_dir = args.output_dir

    os.makedirs(_output_dir, exist_ok=True)

    logger.info("Starting GPU Worker on %s:%d", args.host, args.port)
    logger.info("Models dir: %s", _models_dir)
    logger.info("Output dir: %s", _output_dir)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
