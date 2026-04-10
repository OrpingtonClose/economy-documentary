#!/usr/bin/env python3
"""GPU Worker — FastAPI service for TTS and video generation.

Runs on a Vast.ai GPU VM. Exposes HTTP endpoints that the pipeline
calls to generate narration (Qwen3-TTS) and video clips (LTX-2.3).

Usage:
    python gpu_worker.py [--port 8880] [--models-dir /workspace/models]

Models are expected at:
    {models_dir}/qwen3-tts/     — Qwen3-TTS-12Hz-1.7B-Base
    {models_dir}/ltx2/          — LTX-2.3 distilled checkpoint

The bootstrap script (gpu_bootstrap.sh) downloads these from B2.
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

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
    """Load LTX-2.3 distilled pipeline."""
    global _ltx_pipe
    if _ltx_pipe is not None:
        return

    logger.info("Loading LTX-2.3 distilled from %s ...", _models_dir)
    t0 = time.time()

    # LTX-2.3 uses its own codebase, not diffusers pipeline directly.
    # We use subprocess to call the inference script.
    # Check if ltx2 inference script exists
    ltx_dir = os.path.join(_models_dir, "ltx2")
    if not os.path.isdir(ltx_dir):
        logger.warning("LTX-2.3 model dir not found at %s", ltx_dir)
        return

    # For LTX-2.3, we'll use the inference.py from the official repo
    # which is cloned during bootstrap. We mark as loaded.
    _ltx_pipe = "loaded"
    logger.info("LTX-2.3 ready (%.1fs)", time.time() - t0)


# ---------------------------------------------------------------------------
# TTS generation
# ---------------------------------------------------------------------------

def _generate_tts(text: str, voice: str, language: str) -> tuple[np.ndarray, int]:
    """Generate speech audio using Qwen3-TTS.

    Returns (audio_array, sample_rate).
    """
    _load_tts()

    voice_instruction = _VOICE_PROFILES.get(voice, _VOICE_PROFILES["V1"]).get(
        language, _VOICE_PROFILES["V1"]["en"]
    )

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
# Video generation (LTX-2.3 via subprocess)
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
    """Generate video clip using LTX-2.3 distilled.

    Returns raw MP4 bytes.
    """
    fps = 24
    if num_frames is None:
        # LTX-2.3 works in chunks of 8+1 frames
        raw_frames = int(duration_sec * fps)
        # Round to nearest valid frame count: 8k+1
        num_frames = max(9, ((raw_frames - 1) // 8) * 8 + 1)

    output_path = os.path.join(
        _output_dir, f"clip_{uuid.uuid4().hex[:8]}.mp4"
    )
    os.makedirs(_output_dir, exist_ok=True)

    ltx_dir = os.path.join(_models_dir, "ltx2")
    ltx_code = "/workspace/ltx-video"  # cloned during bootstrap

    # Check if we have the official LTX inference script
    inference_script = os.path.join(ltx_code, "inference.py")
    if not os.path.isfile(inference_script):
        # Fallback: use diffusers pipeline directly
        return _generate_video_diffusers(
            prompt, num_frames, width, height, fps, seed,
            num_inference_steps, guidance_scale, output_path,
        )

    # Use official inference script
    config_path = os.path.join(ltx_code, "configs", "ltxv-13b-0.9.8-distilled.yaml")
    if not os.path.isfile(config_path):
        # Try the LTX-2.3 config
        config_path = os.path.join(ltx_code, "configs", "ltx-2.3-22b-distilled.yaml")

    cmd = [
        "python", inference_script,
        "--config", config_path,
        "--prompt", prompt,
        "--num_frames", str(num_frames),
        "--width", str(width),
        "--height", str(height),
        "--seed", str(seed),
        "--num_inference_steps", str(num_inference_steps),
        "--guidance_scale", str(guidance_scale),
        "--output_path", output_path,
    ]

    logger.info("Running LTX-2.3: %d frames, %dx%d, seed=%d", num_frames, width, height, seed)
    t0 = time.time()

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        cwd=ltx_code,
    )

    elapsed = time.time() - t0
    logger.info("LTX-2.3 finished in %.1fs (rc=%d)", elapsed, result.returncode)

    if result.returncode != 0:
        logger.error("LTX stderr: %s", result.stderr[-2000:])
        raise HTTPException(500, f"LTX-2.3 failed: {result.stderr[-500:]}")

    if not os.path.isfile(output_path):
        raise HTTPException(500, "LTX-2.3 did not produce output file")

    with open(output_path, "rb") as f:
        data = f.read()

    # Clean up
    try:
        os.unlink(output_path)
    except OSError:
        pass

    return data


def _generate_video_diffusers(
    prompt: str,
    num_frames: int,
    width: int,
    height: int,
    fps: int,
    seed: int,
    num_inference_steps: int,
    guidance_scale: float,
    output_path: str,
) -> bytes:
    """Fallback: generate video using diffusers LTXPipeline."""
    from diffusers import LTXPipeline
    from diffusers.utils import export_to_video

    global _ltx_pipe

    model_path = os.path.join(_models_dir, "ltx2")

    if not isinstance(_ltx_pipe, LTXPipeline):
        logger.info("Loading LTX via diffusers from %s ...", model_path)
        _ltx_pipe = LTXPipeline.from_pretrained(
            model_path,
            torch_dtype=torch.bfloat16,
        ).to("cuda")

    generator = torch.Generator("cuda").manual_seed(seed)

    logger.info("Generating video: %d frames, %dx%d", num_frames, width, height)
    t0 = time.time()

    result = _ltx_pipe(
        prompt=prompt,
        num_frames=num_frames,
        width=width,
        height=height,
        num_inference_steps=num_inference_steps,
        guidance_scale=guidance_scale,
        generator=generator,
    )

    elapsed = time.time() - t0
    logger.info("Video generated in %.1fs", elapsed)

    # Export to MP4
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
        vram_total = torch.cuda.get_device_properties(0).total_mem / 1e9

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
