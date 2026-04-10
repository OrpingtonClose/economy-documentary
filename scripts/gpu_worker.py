#!/usr/bin/env python3
"""GPU Worker — FastAPI service for TTS and video generation.

Runs on a Vast.ai GPU VM. Exposes HTTP endpoints that the pipeline
calls to generate narration (Qwen3-TTS) and video clips (LTX-2.3).

Usage:
    python gpu_worker.py [--port 8880] [--models-dir /workspace/models]

Models are expected at:
    {models_dir}/qwen3-tts-voicedesign/  — Qwen3-TTS-12Hz-1.7B-VoiceDesign
    {models_dir}/ltx2/                   — LTX-2.3 diffusers-format components
                                  (model_index.json, text_encoder/, transformer/,
                                   vae/, audio_vae/, vocoder/, connectors/, etc.)

The bootstrap script (gpu_bootstrap.sh) downloads these from B2.
Requires diffusers >= 0.37.0 for LTX2Pipeline support.
bf16 only — no FP8, no quantization.
"""
from __future__ import annotations

import argparse
import base64
import gc
import io
import json
import logging
import os
import threading
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
# Global model handles — model swapping for VRAM management
# ---------------------------------------------------------------------------
_tts_model = None  # Qwen3TTSModel instance
_ltx_pipe = None
_active_model: str = ""  # "tts" or "ltx" — tracks which model is on GPU
_models_dir: str = "/workspace/models"
_output_dir: str = "/workspace/output"
_model_lock = threading.Lock()  # Serialise all model load/unload/inference

# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------

class TTSRequest(BaseModel):
    text: str
    voice: str = "V1"  # V1/V2/V3 — mapped to speaker profiles
    language: str = "en"  # "en" or "ru"
    scene_num: int = 1
    # Note: sample_rate is NOT accepted here — the model's native rate is used
    # and returned via the X-Sample-Rate response header.


class VideoRequest(BaseModel):
    prompt: str
    duration_sec: float = 5.0
    width: int = 768
    height: int = 512
    num_frames: int | None = None  # auto-calculated from duration if None
    seed: int = 42
    num_inference_steps: int = 30  # dev/full model: 20-50 steps
    guidance_scale: float = 3.5  # dev/full model: CFG 2.0-5.0 (recommended 3.0-3.5)


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

def _unload_tts():
    """Move TTS model off GPU and free VRAM."""
    global _tts_model, _active_model
    if _tts_model is None:
        return
    logger.info("Unloading TTS from GPU...")
    del _tts_model
    _tts_model = None
    _active_model = ""
    gc.collect()
    torch.cuda.empty_cache()
    logger.info("TTS unloaded. VRAM free: %.1f GB",
                (torch.cuda.get_device_properties(0).total_memory - torch.cuda.memory_allocated(0)) / 1e9)


def _unload_ltx():
    """Move LTX pipeline off GPU and free VRAM."""
    global _ltx_pipe, _active_model
    if _ltx_pipe is None:
        return
    logger.info("Unloading LTX from GPU...")
    del _ltx_pipe
    _ltx_pipe = None
    _active_model = ""
    gc.collect()
    torch.cuda.empty_cache()
    logger.info("LTX unloaded. VRAM free: %.1f GB",
                (torch.cuda.get_device_properties(0).total_memory - torch.cuda.memory_allocated(0)) / 1e9)


def _load_tts():
    """Load Qwen3-TTS VoiceDesign model via qwen-tts package.

    Unloads LTX first if it's on GPU to free VRAM.
    """
    global _tts_model, _active_model
    if _tts_model is not None:
        return

    # Free VRAM if LTX is loaded
    if _ltx_pipe is not None:
        _unload_ltx()

    from qwen_tts import Qwen3TTSModel

    model_path = os.path.join(_models_dir, "qwen3-tts-voicedesign")
    logger.info("Loading Qwen3-TTS VoiceDesign from %s ...", model_path)
    t0 = time.time()

    _tts_model = Qwen3TTSModel.from_pretrained(
        model_path,
        device_map="cuda:0",
        dtype=torch.bfloat16,
    )
    _active_model = "tts"

    logger.info("Qwen3-TTS VoiceDesign loaded in %.1fs", time.time() - t0)


def _load_ltx():
    """Load LTX-2.3 pipeline via diffusers (>= 0.37.0).

    Unloads TTS first if it's on GPU to free VRAM.
    Uses enable_model_cpu_offload() to keep idle components in CPU RAM
    while the active component runs on GPU. Requires 48GB+ VRAM
    for the Gemma3 text encoder (~24GB bf16 weights).
    """
    global _ltx_pipe, _active_model
    if _ltx_pipe is not None:
        return

    # Free VRAM if TTS is loaded
    if _tts_model is not None:
        _unload_tts()

    from diffusers import LTX2Pipeline

    model_path = os.path.join(_models_dir, "ltx2")
    logger.info("Loading LTX-2.3 via diffusers from %s ...", model_path)
    t0 = time.time()

    if not os.path.isfile(os.path.join(model_path, "model_index.json")):
        raise FileNotFoundError(f"model_index.json not found in {model_path}")

    pipe = LTX2Pipeline.from_pretrained(
        model_path,
        torch_dtype=torch.bfloat16,
    )
    # Model-level CPU offload: entire components move on/off GPU.
    # Requires 48GB+ VRAM (A100 80GB / L40S) to fit the Gemma3 text
    # encoder as a single component. Much faster than sequential offload.
    pipe.enable_model_cpu_offload()
    _ltx_pipe = pipe
    _active_model = "ltx"

    logger.info("LTX-2.3 loaded in %.1fs", time.time() - t0)


# ---------------------------------------------------------------------------
# TTS generation
# ---------------------------------------------------------------------------

def _generate_tts(text: str, voice: str, language: str) -> tuple[np.ndarray, int]:
    """Generate speech audio using Qwen3-TTS VoiceDesign.

    Uses voice instruction text to control the generated voice style.
    Returns (audio_array, sample_rate).
    Caller must hold _model_lock.
    """
    _load_tts()

    profile = _VOICE_PROFILES.get(voice, _VOICE_PROFILES["V1"])
    voice_instruction = profile.get(language, profile.get("en", _VOICE_PROFILES["V1"]["en"]))

    # Map language codes to Qwen3-TTS language names
    lang_map = {"en": "English", "ru": "Russian"}
    tts_language = lang_map.get(language, "Auto")

    logger.info(
        "VoiceDesign: voice=%s lang=%s instruction=%.60s...",
        voice, tts_language, voice_instruction,
    )

    wavs, sr = _tts_model.generate_voice_design(
        text=text,
        instruct=voice_instruction,
        language=tts_language,
    )

    # wavs is a list of np.ndarray; take the first (single utterance)
    audio_array = wavs[0] if wavs else np.zeros(sr, dtype=np.float32)

    return audio_array, sr


# ---------------------------------------------------------------------------
# Video quality validation helpers
# ---------------------------------------------------------------------------

# Minimum thresholds for post-render quality check.
# Brightness: average pixel value (0-255 scale). Below this = too dark.
# Contrast: std dev of pixel values. Below this = flat/washed out.
_MIN_BRIGHTNESS = 40.0  # ~16% of max — very conservative, rejects near-black
_MIN_CONTRAST = 20.0    # rejects flat single-tone frames


def _measure_frame_brightness(frames: list) -> float:
    """Measure average brightness across sampled frames.

    Args:
        frames: List of PIL Images or numpy arrays from diffusers output.

    Returns:
        Average brightness on 0-255 scale.
    """
    if not frames:
        return 0.0

    # Sample up to 5 evenly spaced frames
    n = len(frames)
    indices = [i * (n - 1) // 4 for i in range(5)] if n >= 5 else list(range(n))
    indices = sorted(set(indices))

    total_brightness = 0.0
    for idx in indices:
        frame = frames[idx]
        arr = np.array(frame, dtype=np.float32)
        total_brightness += arr.mean()

    return total_brightness / len(indices)


def _measure_frame_contrast(frames: list) -> float:
    """Measure average contrast (std dev of pixel values) across sampled frames.

    Args:
        frames: List of PIL Images or numpy arrays from diffusers output.

    Returns:
        Average contrast (std dev on 0-255 scale).
    """
    if not frames:
        return 0.0

    n = len(frames)
    indices = [i * (n - 1) // 4 for i in range(5)] if n >= 5 else list(range(n))
    indices = sorted(set(indices))

    total_contrast = 0.0
    for idx in indices:
        frame = frames[idx]
        arr = np.array(frame, dtype=np.float32)
        total_contrast += arr.std()

    return total_contrast / len(indices)


# ---------------------------------------------------------------------------
# Qwen-Omni Visual QA via OpenRouter (bearnaise pattern)
# ---------------------------------------------------------------------------

# OpenRouter API key — set via env var on the GPU VM
_OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")
# Model for video QA — Qwen3.5-Plus supports text, image, video input
_QA_MODEL: str = "qwen/qwen3.5-plus-02-15"


def _frames_to_base64(frames: list, indices: list[int]) -> list[str]:
    """Convert selected PIL frames to base64-encoded JPEG strings."""
    from PIL import Image

    result = []
    for idx in indices:
        frame = frames[idx]
        if not isinstance(frame, Image.Image):
            frame = Image.fromarray(np.array(frame))
        buf = io.BytesIO()
        frame.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        result.append(b64)
    return result


def _qwen_visual_qa(prompt: str, frames_b64: list[str]) -> dict:
    """Send frames to Qwen-Omni via OpenRouter for visual quality assessment.

    Returns dict with keys: quality ("poor"/"good"/"excellent"), qa_reason (str).
    Following bearnaise pattern: per-clip LLM-based visual QA.
    """
    import httpx

    if not _OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY not set — skipping visual QA")
        return {"quality": "unknown", "qa_reason": "No API key for visual QA"}

    # Build multimodal content: frames as images + evaluation prompt
    content_parts = []
    for i, b64 in enumerate(frames_b64):
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    content_parts.append({
        "type": "text",
        "text": (
            f"You are a video quality assessor for AI-generated documentary footage.\n\n"
            f"The video was generated from this prompt:\n"
            f'"{prompt}"\n\n'
            f"These {len(frames_b64)} frames are sampled from the start, middle, and end of the clip.\n\n"
            f"Evaluate the video quality:\n"
            f"1. Does the visual content match what the prompt describes?\n"
            f"2. Is the image clear, well-lit, and visually coherent?\n"
            f"3. Are there artifacts, extreme darkness, blur, or nonsensical imagery?\n\n"
            f"Respond in EXACTLY this JSON format (no markdown, no extra text):\n"
            f'{{"quality": "poor|good|excellent", "qa_reason": "brief description of what the video shows and why you rated it this way"}}'
        ),
    })

    try:
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": _QA_MODEL,
                "messages": [{"role": "user", "content": content_parts}],
                "max_tokens": 300,
                "temperature": 0.1,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()

        # Parse JSON from response — handle markdown fencing
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)
        quality = result.get("quality", "unknown").lower()
        qa_reason = result.get("qa_reason", "No reason provided")

        if quality not in ("poor", "good", "excellent"):
            quality = "unknown"

        return {"quality": quality, "qa_reason": qa_reason}

    except Exception as e:
        logger.error("Visual QA failed: %s", e, exc_info=True)
        return {"quality": "unknown", "qa_reason": f"QA request failed: {e}"}


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
) -> tuple[bytes, dict]:
    """Generate video clip using LTX-2.3 dev (full) model via diffusers.

    Returns (raw_mp4_bytes, qa_status) where qa_status follows bearnaise
    pattern: {quality, qa_reason, attempts, seed}.
    Caller must hold _model_lock.
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
        "static, low resolution, dark, underexposed"
    )

    logger.info(
        "Generating video: %d frames (%.1fs), %dx%d, seed=%d",
        num_frames, num_frames / fps, width, height, seed,
    )
    t0 = time.time()

    # Retry loop with Qwen-Omni visual QA (bearnaise pattern).
    # After each generation: brightness/contrast check first (fast, free),
    # then Qwen-Omni visual QA for semantic evaluation.
    max_attempts = 3
    current_seed = seed
    best_frames = None
    best_score = -1.0
    best_seed = seed
    best_qa: dict = {"quality": "unknown", "qa_reason": "Not evaluated"}
    final_attempt = 0

    for attempt in range(1, max_attempts + 1):
        final_attempt = attempt
        gen = torch.Generator("cpu").manual_seed(current_seed)
        result = _ltx_pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            num_frames=num_frames,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            generator=gen,
        )
        candidate_frames = result.frames[0]

        # Stage 1: Fast brightness/contrast check (free, instant)
        brightness = _measure_frame_brightness(candidate_frames)
        contrast = _measure_frame_contrast(candidate_frames)
        score = brightness + contrast
        logger.info(
            "Brightness check (attempt %d/%d): brightness=%.1f/255, contrast=%.1f, seed=%d",
            attempt, max_attempts, brightness, contrast, current_seed,
        )

        if brightness < _MIN_BRIGHTNESS or contrast < _MIN_CONTRAST:
            logger.warning(
                "Video too dark/flat — skipping Qwen QA, retrying..."
            )
            if score > best_score:
                best_frames = candidate_frames
                best_score = score
                best_seed = current_seed
            if attempt < max_attempts:
                current_seed = (current_seed + 7919) % (2**31)
                continue
            else:
                break

        # Keep best by brightness/contrast score
        if score > best_score:
            best_frames = candidate_frames
            best_score = score
            best_seed = current_seed

        # Stage 2: Qwen-Omni visual QA (semantic evaluation)
        n = len(candidate_frames)
        sample_indices = [0, n // 2, n - 1] if n >= 3 else list(range(n))
        frames_b64 = _frames_to_base64(candidate_frames, sample_indices)
        qa_result = _qwen_visual_qa(prompt, frames_b64)

        logger.info(
            "Qwen QA (attempt %d/%d): quality=%s, reason=%.120s",
            attempt, max_attempts, qa_result["quality"],
            qa_result.get("qa_reason", ""),
        )

        if qa_result["quality"] in ("good", "excellent"):
            best_frames = candidate_frames
            best_seed = current_seed
            best_qa = qa_result
            break

        # QA says poor or unknown — update best and retry
        best_qa = qa_result
        if attempt < max_attempts:
            logger.warning(
                "Qwen QA rated '%s' — retrying with new seed...",
                qa_result["quality"],
            )
            current_seed = (current_seed + 7919) % (2**31)
        else:
            logger.warning(
                "Video still rated '%s' after %d attempts. Using best result.",
                qa_result["quality"], max_attempts,
            )

    video_frames = best_frames

    elapsed = time.time() - t0
    logger.info("Video generated in %.1fs (seed=%d, qa=%s)",
                elapsed, best_seed, best_qa.get("quality", "unknown"))

    # Export to MP4
    output_path = os.path.join(
        _output_dir, f"clip_{uuid.uuid4().hex[:8]}.mp4"
    )
    os.makedirs(_output_dir, exist_ok=True)

    export_to_video(video_frames, output_path, fps=fps)

    with open(output_path, "rb") as f:
        data = f.read()

    try:
        os.unlink(output_path)
    except OSError:
        pass

    # Bearnaise-style per-clip QA status
    qa_status = {
        "quality": best_qa.get("quality", "unknown"),
        "qa_reason": best_qa.get("qa_reason", "Not evaluated"),
        "attempts": final_attempt,
        "seed": best_seed,
        "brightness": round(best_score - _measure_frame_contrast(video_frames), 1) if video_frames else 0,
        "contrast": round(_measure_frame_contrast(video_frames), 1) if video_frames else 0,
    }

    return data, qa_status


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
    if not req.text.strip():
        raise HTTPException(400, "Text must not be empty")

    logger.info(
        "TTS request: scene=%d voice=%s lang=%s text=%d chars",
        req.scene_num, req.voice, req.language, len(req.text),
    )
    t0 = time.time()

    with _model_lock:
        try:
            audio_array, sr = _generate_tts(req.text, req.voice, req.language)
        except Exception as e:
            logger.error("TTS failed: %s", e, exc_info=True)
            raise HTTPException(500, f"TTS generation failed: {e}")

    # Encode as WAV (no lock needed — local data only)
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
    if not req.prompt.strip():
        raise HTTPException(400, "Prompt must not be empty")
    if req.duration_sec <= 0:
        raise HTTPException(400, "duration_sec must be positive")
    if req.width <= 0 or req.height <= 0:
        raise HTTPException(400, "width and height must be positive")

    logger.info(
        "Video request: %.1fs, %dx%d, seed=%d, prompt=%.100s...",
        req.duration_sec, req.width, req.height, req.seed, req.prompt,
    )
    t0 = time.time()

    with _model_lock:
        try:
            mp4_bytes, qa_status = _generate_video(
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
    logger.info(
        "Video done: %d bytes in %.1fs, qa=%s",
        len(mp4_bytes), elapsed, qa_status.get("quality", "unknown"),
    )

    return Response(
        content=mp4_bytes,
        media_type="video/mp4",
        headers={
            "X-Gen-Time": str(round(elapsed, 3)),
            "X-File-Size": str(len(mp4_bytes)),
            "X-QA-Quality": qa_status.get("quality", "unknown"),
            "X-QA-Reason": qa_status.get("qa_reason", "")[:200],
            "X-QA-Attempts": str(qa_status.get("attempts", 1)),
            "X-QA-Seed": str(qa_status.get("seed", req.seed)),
        },
    )


@app.post("/load-models")
def load_models(model: str = "tts"):
    """Load a specific model into VRAM (unloads the other first).

    Args:
        model: Which model to load — "tts" or "ltx". Default: "tts".
    """
    results = {}

    with _model_lock:
        if model == "tts":
            try:
                _load_tts()
                results["tts"] = "loaded"
                results["ltx"] = "unloaded (VRAM constraint)"
            except Exception as e:
                results["tts"] = f"error: {e}"
        elif model == "ltx":
            try:
                _load_ltx()
                results["ltx"] = "loaded"
                results["tts"] = "unloaded (VRAM constraint)"
            except Exception as e:
                results["ltx"] = f"error: {e}"
        else:
            results["error"] = f"Unknown model: {model}. Use 'tts' or 'ltx'."

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
