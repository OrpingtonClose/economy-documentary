#!/usr/bin/env python3
"""GPU Worker — FastAPI service for TTS and video generation.

Runs on a Vast.ai GPU VM. Exposes HTTP endpoints that the pipeline
calls to generate narration (Qwen3-TTS) and video clips (LTX-2.3).

Usage:
    python gpu_worker.py --mode tts  [--port 8880] [--models-dir ...]  # TTS-only VM
    python gpu_worker.py --mode ltx  [--port 8880] [--models-dir ...]  # LTX-only VM
    python gpu_worker.py --mode both [--port 8880] [--models-dir ...]  # shared (legacy)

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
_worker_mode: str = "both"  # "tts", "ltx", or "both"

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
    negative_prompt: str = ""  # per-clip negative prompt from visual_style.avoid
    visual_style: str = ""  # movie-level visual style description for QA
    duration_sec: float = 5.0
    width: int = 768
    height: int = 512
    num_frames: int | None = None  # auto-calculated from duration if None
    seed: int = 42
    num_inference_steps: int = 40  # full model: 40 steps (pipeline default)
    guidance_scale: float = 4.0  # full model: CFG=4.0 (pipeline default)


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

    Always unloads LTX first if loaded — prevents OOM from both models
    coexisting in VRAM.  In single-mode (--mode tts) LTX should never be
    loaded, so the guard is a safety net rather than normal path.
    """
    global _tts_model, _active_model
    if _tts_model is not None:
        return

    # Always free VRAM if LTX is loaded — OOM safety net
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

    Always unloads TTS first if loaded — prevents OOM from both models
    coexisting in VRAM.  In single-mode (--mode ltx) TTS should never be
    loaded, so the guard is a safety net rather than normal path.
    Loads all components fully on GPU (no cpu_offload). Requires 80GB+ VRAM
    (model is ~71GB bf16). H200 (141GB) or A100 80GB recommended.
    """
    global _ltx_pipe, _active_model
    if _ltx_pipe is not None:
        return

    # Always free VRAM if TTS is loaded — OOM safety net
    if _tts_model is not None:
        _unload_tts()

    from diffusers import LTX2Pipeline

    # Try models_dir/ltx2 first (standard layout), then models_dir itself
    # (when --models-dir points directly at the model directory).
    candidate = os.path.join(_models_dir, "ltx2")
    if os.path.isfile(os.path.join(candidate, "model_index.json")):
        model_path = candidate
    elif os.path.isfile(os.path.join(_models_dir, "model_index.json")):
        model_path = _models_dir
    else:
        raise FileNotFoundError(
            f"model_index.json not found in {candidate} or {_models_dir}"
        )
    logger.info("Loading LTX-2.3 via diffusers from %s ...", model_path)
    t0 = time.time()

    pipe = LTX2Pipeline.from_pretrained(
        model_path,
        torch_dtype=torch.bfloat16,
        low_cpu_mem_usage=False,
    ).to("cuda")
    # NOTE: VAE tiling disabled — it causes grid artifacts at 768x512.
    # The H200 NVL has 140GB VRAM, so tiling is unnecessary.
    # pipe.vae.enable_tiling()
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
_MIN_BRIGHTNESS = 20.0  # ~8% of max — only rejects near-black frames
_MIN_CONTRAST = 3.0     # only rejects truly flat/single-tone frames; cinematic footage can be low-contrast


def _measure_frame_brightness(frames) -> float:
    """Measure average brightness across sampled frames.

    Args:
        frames: numpy array (T, H, W, C) in [0,1] float range,
                or list of PIL Images / numpy arrays.

    Returns:
        Average brightness on 0-255 scale.
    """
    if frames is None:
        return 0.0
    arr = np.asarray(frames, dtype=np.float32)
    if arr.ndim == 4:
        # (T, H, W, C) — sample up to 5 evenly spaced frames
        n = arr.shape[0]
        indices = [i * (n - 1) // 4 for i in range(5)] if n >= 5 else list(range(n))
        indices = sorted(set(indices))
        sampled = arr[indices]
    elif arr.ndim == 3:
        sampled = arr[np.newaxis]  # single frame
    else:
        return 0.0
    # Scale to 0-255 if in [0,1] float range (use 1.5 threshold —
    # VAE decode can produce values slightly > 1.0 due to fp imprecision)
    if sampled.size == 0:
        return 0.0
    if sampled.max() <= 1.5:
        sampled = sampled * 255.0
    return float(sampled.mean())


def _measure_frame_contrast(frames) -> float:
    """Measure average contrast (std dev of pixel values) across sampled frames.

    Args:
        frames: numpy array (T, H, W, C) in [0,1] float range,
                or list of PIL Images / numpy arrays.

    Returns:
        Average contrast (std dev on 0-255 scale).
    """
    if frames is None:
        return 0.0
    arr = np.asarray(frames, dtype=np.float32)
    if arr.ndim == 4:
        n = arr.shape[0]
        indices = [i * (n - 1) // 4 for i in range(5)] if n >= 5 else list(range(n))
        indices = sorted(set(indices))
        sampled = arr[indices]
    elif arr.ndim == 3:
        sampled = arr[np.newaxis]
    else:
        return 0.0
    if sampled.size == 0:
        return 0.0
    if sampled.max() <= 1.5:
        sampled = sampled * 255.0
    return float(np.mean([sampled[i].std() for i in range(sampled.shape[0])]))


# ---------------------------------------------------------------------------
# Qwen-Omni Visual QA via DashScope (bearnaise pattern)
# ---------------------------------------------------------------------------

# DashScope API key — set via env var on the GPU VM
# Falls back to OPENROUTER_API_KEY for backward compatibility.
_DASHSCOPE_API_KEY: str = os.environ.get("DASHSCOPE_API_KEY", "")
_OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")
# Which backend to use: DashScope (preferred) or OpenRouter (fallback)
_QA_BACKEND: str = "dashscope" if _DASHSCOPE_API_KEY else ("openrouter" if _OPENROUTER_API_KEY else "")
# Model for video QA — Qwen VL model with vision capabilities
_QA_MODEL_DASHSCOPE: str = "qwen-vl-max"  # Qwen-VL-Max on DashScope
_QA_MODEL_OPENROUTER: str = "qwen/qwen3.5-plus-02-15"  # fallback on OpenRouter


def _frames_to_base64(frames, indices: list[int]) -> list[str]:
    """Convert selected frames to base64-encoded JPEG strings.

    Args:
        frames: numpy array (T, H, W, C) in [0,1] float or uint8,
                or list of PIL Images.
    """
    from PIL import Image

    arr = np.asarray(frames)
    result = []
    for idx in indices:
        frame = arr[idx]
        # Convert [0,1] float to uint8 if needed
        if frame.dtype != np.uint8:
            frame = (np.clip(frame, 0, 1) * 255).astype(np.uint8)
        img = Image.fromarray(frame)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        result.append(b64)
    return result


def _qwen_visual_qa(prompt: str, frames_b64: list[str], visual_style: str = "") -> dict:
    """Send frames to Qwen-Omni via OpenRouter for visual quality assessment.

    Returns dict with keys: quality ("poor"/"good"/"excellent"), qa_reason (str).
    Following bearnaise pattern: per-clip LLM-based visual QA.

    Args:
        prompt: The generation prompt for this clip.
        frames_b64: Base64-encoded JPEG frames (start, middle, end).
        visual_style: Movie-level visual style description. When provided,
            QA checks that the clip conforms to the film's declared aesthetic.
    """
    import httpx

    if not _QA_BACKEND:
        raise RuntimeError(
            "OTIO VIOLATION: visual QA unavailable — neither DASHSCOPE_API_KEY "
            "nor OPENROUTER_API_KEY is set. QA is mandatory; the pipeline "
            "cannot accept clips without quality verification."
        )

    # Build multimodal content: frames as images + evaluation prompt
    content_parts = []
    for i, b64 in enumerate(frames_b64):
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    # Build the style enforcement section
    style_section = ""
    if visual_style:
        style_section = (
            f"\nMOVIE-LEVEL VISUAL STYLE (the entire film must look like this):\n"
            f"{visual_style}\n\n"
            f"CHECK: Does this clip conform to the movie's declared visual style?\n"
            f"If the movie style says 'photorealistic' but the clip looks like a \n"
            f"cartoon, illustration, or CGI render, rate it POOR regardless of \n"
            f"other qualities.\n"
        )

    content_parts.append({
        "type": "text",
        "text": (
            f"You are a video quality assessor for AI-generated footage.\n\n"
            f"The video was generated from this prompt:\n"
            f'"{prompt}"\n'
            f"{style_section}\n"
            f"These {len(frames_b64)} frames are sampled from the start, middle, and end of the clip.\n\n"
            f"Evaluate the video quality:\n"
            f"1. Does the visual content match what the prompt describes?\n"
            f"2. Does the clip match the movie's visual style? (most important)\n"
            f"3. Are there blatant AI artifacts: distorted shapes, impossible \n"
            f"   geometry, morphing, grid patterns, or incoherent motion?\n"
            f"4. Is the output clearly the WRONG medium (e.g. cartoon when the \n"
            f"   movie style requires photorealism)?\n\n"
            f"Be LENIENT on minor imperfections (slight blur, small lighting \n"
            f"differences). Only rate POOR for blatant failures: wrong medium, \n"
            f"AI wonkiness, or complete prompt mismatch.\n\n"
            f"Respond in EXACTLY this JSON format (no markdown, no extra text):\n"
            f'{{"quality": "poor|good|excellent", "qa_reason": "brief description of what the video shows and why you rated it this way"}}'
        ),
    })

    try:
        # Select backend: DashScope (preferred) or OpenRouter (fallback)
        if _QA_BACKEND == "dashscope":
            api_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
            api_key = _DASHSCOPE_API_KEY
            model = _QA_MODEL_DASHSCOPE
        else:
            api_url = "https://openrouter.ai/api/v1/chat/completions"
            api_key = _OPENROUTER_API_KEY
            model = _QA_MODEL_OPENROUTER

        logger.info("Visual QA using %s (model=%s)", _QA_BACKEND, model)
        resp = httpx.post(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
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
    negative_prompt: str = "",
    visual_style: str = "",
) -> tuple[bytes, dict]:
    """Generate video clip using LTX-2.3 dev (full) model via diffusers.

    Returns (raw_mp4_bytes, qa_status) where qa_status follows bearnaise
    pattern: {quality, qa_reason, attempts, seed}.
    Caller must hold _model_lock.

    Args:
        negative_prompt: Per-clip negative prompt from visual_style.avoid.
            Merged with baseline negatives.
        visual_style: Movie-level visual style description passed to QA.
    """
    _load_ltx()

    from diffusers.utils import export_to_video

    fps = 24
    if num_frames is None:
        # LTX-2.3 works in chunks of 8+1 frames
        raw_frames = int(duration_sec * fps)
        # Round to nearest valid frame count: 8k+1
        num_frames = max(9, ((raw_frames - 1) // 8) * 8 + 1)

    # Baseline negatives + per-clip negatives from visual_style.avoid
    baseline_negatives = (
        "worst quality, inconsistent motion, blurry, jittery, distorted, "
        "static, low resolution, morphing, warping, flicker, "
        "text, watermark, logo"
    )
    if negative_prompt:
        negative_prompt = f"{negative_prompt}, {baseline_negatives}"
    else:
        negative_prompt = baseline_negatives

    logger.info(
        "Generating video: %d frames (%.1fs), %dx%d, seed=%d",
        num_frames, num_frames / fps, width, height, seed,
    )
    t0 = time.time()

    # Retry loop with Qwen-Omni visual QA (bearnaise pattern).
    # After each generation: brightness/contrast check first (fast, free),
    # then Qwen-Omni visual QA for semantic evaluation.
    #
    # Tracks passing (brightness OK) and failing results separately so that
    # any brightness-passing frame is always preferred over a failing one,
    # even if the failing frame has a higher raw score.
    max_attempts = 3
    current_seed = seed
    # Best result that passed brightness/contrast thresholds
    best_passing_frames = None
    best_passing_audio = None
    best_passing_score = -1.0
    best_passing_seed = seed
    best_passing_qa: dict = {"quality": "unknown", "qa_reason": "Not evaluated"}
    # Best result among brightness-failing attempts (fallback only)
    best_failing_frames = None
    best_failing_audio = None
    best_failing_score = -1.0
    best_failing_seed = seed
    final_attempt = 0

    for attempt in range(1, max_attempts + 1):
        final_attempt = attempt
        gen = torch.Generator("cuda").manual_seed(current_seed)
        # Use pipeline defaults for stg/modality/rescale — these are tuned
        # for the full (non-distilled) LTX-2.3 model.
        # output_type "np": proper numpy output for frame processing
        video_out, audio_out = _ltx_pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            num_frames=num_frames,
            frame_rate=fps,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            generator=gen,
            output_type="np",
            return_dict=False,
        )
        candidate_frames = video_out[0]  # numpy array (T, H, W, C)
        candidate_audio = audio_out[0] if audio_out is not None else None

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
            # Track best among failing attempts (fallback only)
            if score > best_failing_score:
                best_failing_frames = candidate_frames
                best_failing_audio = candidate_audio
                best_failing_score = score
                best_failing_seed = current_seed
            if attempt < max_attempts:
                current_seed = (current_seed + 7919) % (2**31)
                continue
            else:
                break

        # Brightness passed — track among passing attempts.
        # QA is paired with frames so metadata always describes the selected output.
        is_new_best = score > best_passing_score
        if is_new_best:
            best_passing_frames = candidate_frames
            best_passing_audio = candidate_audio
            best_passing_score = score
            best_passing_seed = current_seed

        # Stage 2: Qwen-Omni visual QA (semantic evaluation)
        n = candidate_frames.shape[0] if hasattr(candidate_frames, 'shape') else len(candidate_frames)
        sample_indices = [0, n // 2, n - 1] if n >= 3 else list(range(n))
        frames_b64 = _frames_to_base64(candidate_frames, sample_indices)
        qa_result = _qwen_visual_qa(prompt, frames_b64, visual_style=visual_style)

        logger.info(
            "Qwen QA (attempt %d/%d): quality=%s, reason=%.120s",
            attempt, max_attempts, qa_result["quality"],
            qa_result.get("qa_reason", ""),
        )

        if qa_result["quality"] in ("good", "excellent"):
            best_passing_frames = candidate_frames
            best_passing_audio = candidate_audio
            best_passing_seed = current_seed
            best_passing_qa = qa_result
            break

        # QA says poor or unknown — only update QA if these are the best frames
        if is_new_best:
            best_passing_qa = qa_result
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

    # Prefer any brightness-passing frame over a brightness-failing one
    if best_passing_frames is not None:
        video_frames = best_passing_frames
        video_audio = best_passing_audio
        best_seed = best_passing_seed
        best_qa = best_passing_qa
    else:
        video_frames = best_failing_frames
        video_audio = best_failing_audio
        best_seed = best_failing_seed
        best_qa = {"quality": "unknown", "qa_reason": "All attempts failed brightness check"}

    elapsed = time.time() - t0
    logger.info("Video generated in %.1fs (seed=%d, qa=%s)",
                elapsed, best_seed, best_qa.get("quality", "unknown"))

    # Export to MP4
    output_path = os.path.join(
        _output_dir, f"clip_{uuid.uuid4().hex[:8]}.mp4"
    )
    os.makedirs(_output_dir, exist_ok=True)

    # Use diffusers encode_video for proper MP4 encoding (with audio)
    try:
        from diffusers.pipelines.ltx2.export_utils import encode_video as ltx_encode_video
        # encode_video requires audio and audio_sample_rate
        audio_sr = 44100  # LTX-2.3 default audio sample rate
        if video_audio is not None:
            ltx_encode_video(
                video_frames,
                audio=video_audio,
                audio_sample_rate=audio_sr,
                fps=fps,
                output_path=output_path,
            )
        else:
            ltx_encode_video(
                video_frames,
                fps=fps,
                output_path=output_path,
            )
    except (ImportError, Exception) as exc:
        logger.warning("encode_video failed (%s), falling back to export_to_video", exc)
        # Fallback: convert numpy [0,1] to uint8 PIL frames for export_to_video
        if isinstance(video_frames, np.ndarray) and video_frames.dtype != np.uint8:
            from PIL import Image
            pil_frames = [
                Image.fromarray((np.clip(f, 0, 1) * 255).astype(np.uint8))
                for f in video_frames
            ]
            export_to_video(pil_frames, output_path, fps=fps)
        else:
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
        "brightness": round(_measure_frame_brightness(video_frames), 1) if video_frames is not None else 0,
        "contrast": round(_measure_frame_contrast(video_frames), 1) if video_frames is not None else 0,
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
    if _worker_mode == "ltx":
        raise HTTPException(503, "This worker is in LTX-only mode. Use the TTS worker.")
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
    if _worker_mode == "tts":
        raise HTTPException(503, "This worker is in TTS-only mode. Use the video worker.")
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
                negative_prompt=req.negative_prompt,
                visual_style=req.visual_style,
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
            "X-QA-Reason": base64.b64encode(
                qa_status.get("qa_reason", "")[:500].encode("utf-8")
            ).decode("ascii"),
            "X-QA-Attempts": str(qa_status.get("attempts", 1)),
            "X-QA-Seed": str(qa_status.get("seed", req.seed)),
        },
    )


@app.post("/load-models")
def load_models(model: str = "tts"):
    """Load a specific model into VRAM (unloads the other first).

    In single-mode workers, rejects requests for the wrong model type
    to prevent accidentally loading both models and causing OOM.

    Args:
        model: Which model to load — "tts" or "ltx". Default: "tts".
    """
    # Reject wrong-model requests in single-mode workers
    if _worker_mode == "tts" and model == "ltx":
        raise HTTPException(503, "This worker is in TTS-only mode. Cannot load LTX.")
    if _worker_mode == "ltx" and model == "tts":
        raise HTTPException(503, "This worker is in LTX-only mode. Cannot load TTS.")

    results = {}

    with _model_lock:
        if model == "tts":
            try:
                _load_tts()
                results["tts"] = "loaded"
                results["ltx"] = "unloaded" if _ltx_pipe is None else "still loaded (unexpected)"
            except Exception as e:
                results["tts"] = f"error: {e}"
        elif model == "ltx":
            try:
                _load_ltx()
                results["ltx"] = "loaded"
                results["tts"] = "unloaded" if _tts_model is None else "still loaded (unexpected)"
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
    parser.add_argument(
        "--mode", type=str, default="both",
        choices=["tts", "ltx", "both"],
        help=(
            "Which model to serve. 'tts' = Qwen3-TTS only (cheap GPU), "
            "'ltx' = LTX-2.3 only (A100), 'both' = shared (legacy, not "
            "recommended — model swapping causes lock contention)."
        ),
    )
    args = parser.parse_args()

    global _models_dir, _output_dir, _worker_mode
    _models_dir = args.models_dir
    _output_dir = args.output_dir
    _worker_mode = args.mode

    os.makedirs(_output_dir, exist_ok=True)

    logger.info("Starting GPU Worker on %s:%d (mode=%s)", args.host, args.port, _worker_mode)
    logger.info("Models dir: %s", _models_dir)
    logger.info("Output dir: %s", _output_dir)

    # Pre-load models in a background thread so uvicorn starts immediately.
    # The /health endpoint reports tts_loaded/ltx_loaded status, so callers
    # can poll until the model they need is ready.
    import threading

    def _background_preload():
        if _worker_mode == "tts":
            try:
                with _model_lock:
                    _load_tts()
            except Exception as e:
                logger.error("Failed to pre-load TTS: %s", e, exc_info=True)
        if _worker_mode in ("ltx", "both"):
            try:
                with _model_lock:
                    _load_ltx()
            except Exception as e:
                logger.error("Failed to pre-load LTX: %s", e, exc_info=True)

    preload_thread = threading.Thread(target=_background_preload, daemon=True)
    preload_thread.start()
    logger.info("Model pre-loading started in background thread")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
