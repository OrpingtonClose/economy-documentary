#!/usr/bin/env python3
"""GPU Worker — autonomous VM agent for TTS and video generation.

Each VM runs one mode: tts, ltx, or both.  Two endpoints only.
"""
from __future__ import annotations

import os

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import argparse
import base64
import gc
import io
import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import ctypes as _ctypes
import glob as _glob
try:
    import site as _site
    _search_dirs = []
    for _sp in _site.getsitepackages():
        _nv = os.path.join(_sp, "nvidia")
        if os.path.isdir(_nv):
            for _sub in os.listdir(_nv):
                _lib = os.path.join(_nv, _sub, "lib")
                if os.path.isdir(_lib):
                    _search_dirs.append(_lib)
    _usp = _site.getusersitepackages()
    if isinstance(_usp, str):
        _nv = os.path.join(_usp, "nvidia")
        if os.path.isdir(_nv):
            for _sub in os.listdir(_nv):
                _lib = os.path.join(_nv, _sub, "lib")
                if os.path.isdir(_lib):
                    _search_dirs.append(_lib)
    if _search_dirs:
        _ld = os.environ.get("LD_LIBRARY_PATH", "")
        _new_paths = ":".join(d for d in _search_dirs if d not in _ld)
        if _new_paths:
            os.environ["LD_LIBRARY_PATH"] = f"{_new_paths}:{_ld}" if _ld else _new_paths
    for _d in _search_dirs:
        for _so in sorted(_glob.glob(os.path.join(_d, "libcudart*.so*"))):
            try:
                _ctypes.CDLL(_so, mode=_ctypes.RTLD_GLOBAL)
            except OSError:
                pass
    del _search_dirs, _site
except Exception as exc:
    logger.warning("CUDA lib preload failed: %s", exc)
del _ctypes, _glob

import numpy as np
import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("gpu_worker")

app = FastAPI(title="Documentary GPU Worker")


# ---------------------------------------------------------------------------
# Bootstrap status
# ---------------------------------------------------------------------------

class BootstrapStatus:
    phase: str = "idle"
    detail: str = ""
    error: str = ""
    error_category: str = ""
    started_at: float = 0.0
    completed_at: float = 0.0


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class EscalationEvent:
    timestamp: float
    severity: str
    source: str
    message: str
    details: dict = field(default_factory=dict)
    acked: bool = False

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "severity": self.severity,
            "source": self.source,
            "message": self.message,
            "details": self.details,
            "acked": self.acked,
        }


@dataclass
class HealthSnapshot:
    gpu_name: str = ""
    vram_used_gb: float = 0.0
    vram_total_gb: float = 0.0
    vram_pct: float = 0.0
    gpu_temp_c: float = 0.0
    disk_free_gb: float = 0.0
    disk_total_gb: float = 0.0
    disk_pct: float = 0.0
    checked_at: float = 0.0


class VMAgent:
    def __init__(self, worker_mode: str, models_dir: str, output_dir: str, monitor_interval: float = 30.0) -> None:
        self.worker_mode = worker_mode
        self.models_dir = models_dir
        self.output_dir = output_dir
        self._monitor_interval = monitor_interval
        self.bootstrap = BootstrapStatus()
        self._health: HealthSnapshot = HealthSnapshot()
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._escalations: list[EscalationEvent] = []
        self._tasks_completed: int = 0
        self._tasks_failed: int = 0
        self._total_gen_time: float = 0.0
        self._last_activity: float = time.time()
        self._max_idle_seconds: float = 15.0 * 60.0

    def run_bootstrap(self) -> bool:
        self.bootstrap.started_at = time.time()
        bootstrap_needed = False
        if self.worker_mode in ("tts", "both"):
            marker = os.path.join(self.models_dir, "qwen3-tts-voicedesign", "model.safetensors")
            if not os.path.isfile(marker):
                bootstrap_needed = True
        if self.worker_mode in ("ltx", "both"):
            marker = os.path.join(self.models_dir, "ltx2", "ltx-2.3-22b-dev.safetensors")
            if not os.path.isfile(marker):
                bootstrap_needed = True

        if bootstrap_needed:
            ok = self._run_bootstrap_script()
            if not ok:
                return False

        ok = self._load_models()
        if not ok:
            return False

        self.bootstrap.phase = "ready"
        self.bootstrap.detail = "All models loaded"
        self.bootstrap.completed_at = time.time()
        return True

    def _run_bootstrap_script(self) -> bool:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        bootstrap_script = os.path.join(script_dir, "gpu_bootstrap.sh")
        if not os.path.isfile(bootstrap_script):
            self._fail_bootstrap(f"Bootstrap script not found at {bootstrap_script}", "missing_file")
            return False

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            self.bootstrap.phase = "deps" if attempt == 1 else "models"
            self.bootstrap.detail = f"Running bootstrap (attempt {attempt}/{max_retries})"
            logger.info("VMAgent: bootstrap attempt %d/%d", attempt, max_retries)
            try:
                env = os.environ.copy()
                env["WORKER_MODE"] = self.worker_mode
                result = subprocess.run(
                    ["bash", bootstrap_script],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    logger.info("VMAgent: bootstrap succeeded on attempt %d", attempt)
                    return True
                stderr = result.stderr[-2000:] if result.stderr else ""
                error_cat = self._categorise_error(stderr)
                if error_cat == "network" and attempt < max_retries:
                    wait = 15 * attempt
                    self.escalate(
                        Severity.WARNING, "bootstrap",
                        f"Bootstrap attempt {attempt} failed (network) — retrying in {wait}s",
                    )
                    time.sleep(wait)
                    continue
                self._fail_bootstrap(f"Bootstrap failed: {stderr[-500:]}", error_cat)
                return False
            except Exception as e:
                self._fail_bootstrap(f"Bootstrap exception: {e}", "runtime")
                return False
        self._fail_bootstrap("Bootstrap exhausted all retries", "runtime")
        return False

    def _load_models(self) -> bool:
        self.bootstrap.phase = "loading"
        self.bootstrap.detail = "Loading models into VRAM"
        if self.worker_mode in ("tts", "both"):
            try:
                with _model_lock:
                    _load_tts()
            except Exception as e:
                self._fail_bootstrap(f"Failed to load TTS model: {e}", "runtime")
                return False
        if self.worker_mode in ("ltx", "both"):
            try:
                with _model_lock:
                    _load_ltx()
            except Exception as e:
                self._fail_bootstrap(f"Failed to load LTX model: {e}", "runtime")
                return False
        return True

    def _fail_bootstrap(self, error: str, category: str) -> None:
        self.bootstrap.phase = "error"
        self.bootstrap.error = error
        self.bootstrap.error_category = category
        logger.error("VMAgent BOOTSTRAP FAILED (%s): %s", category, error)
        self.escalate(Severity.CRITICAL, "bootstrap", error, {"category": category})

    @staticmethod
    def _categorise_error(combined: str) -> str:
        if "401" in combined or "403" in combined or "GatedRepo" in combined:
            return "auth"
        if "404" in combined or "EntryNotFound" in combined:
            return "missing_file"
        if "No space left" in combined or "disk" in combined.lower():
            return "disk"
        if "ConnectionError" in combined or "timeout" in combined.lower():
            return "network"
        return "runtime"

    def start_monitoring(self) -> None:
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._shutdown.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="vm-agent-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def stop_monitoring(self) -> None:
        self._shutdown.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5.0)

    def _monitor_loop(self) -> None:
        self._check_health()
        while not self._shutdown.is_set():
            if self._shutdown.wait(timeout=self._monitor_interval):
                break
            self._check_health()

    def record_activity(self) -> None:
        """Reset deadman timer on any substantial activity."""
        self._last_activity = time.time()

    def _check_health(self) -> None:
        idle_seconds = time.time() - self._last_activity
        if idle_seconds > self._max_idle_seconds:
            logger.critical("DEADMAN: No overseer contact for %.0f min. Shutting down.", idle_seconds / 60.0)
            try:
                inst_id = os.environ.get("VAST_CONTAINERLABEL", "").split("_")[-1]
                if not inst_id or not inst_id.isdigit():
                    inst_id = os.environ.get("INSTANCE_ID", "")
                if inst_id and inst_id.isdigit():
                    api_key = os.environ.get("VAST_API_KEY", "")
                    if api_key:
                        subprocess.run(
                            ["vastai", "--api-key", api_key, "destroy", "instance", inst_id],
                            capture_output=True,
                        )
            except Exception as e:
                logger.error("DEADMAN: Failed to destroy instance: %s", e)
            os._exit(1)

        snap = HealthSnapshot(checked_at=time.time())
        if torch.cuda.is_available():
            snap.gpu_name = torch.cuda.get_device_name(0)
            snap.vram_used_gb = torch.cuda.memory_allocated(0) / 1e9
            snap.vram_total_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
            snap.vram_pct = snap.vram_used_gb / max(snap.vram_total_gb, 0.01) * 100
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                snap.gpu_temp_c = float(result.stdout.strip().split("\n")[0])
        except Exception as exc:
            logger.warning("GPU temp read failed: %s", exc)
        try:
            usage = shutil.disk_usage(self.models_dir)
            snap.disk_free_gb = usage.free / (1024**3)
            snap.disk_total_gb = usage.total / (1024**3)
            snap.disk_pct = usage.used / max(usage.total, 1) * 100
        except Exception as exc:
            logger.warning("Disk usage read failed: %s", exc)

        with self._lock:
            self._health = snap

        if snap.vram_pct > 95:
            self.escalate(Severity.WARNING, "gpu", f"VRAM pressure: {snap.vram_pct:.0f}% used")
        if snap.gpu_temp_c > 85:
            self.escalate(Severity.WARNING, "gpu", f"GPU temperature high: {snap.gpu_temp_c:.0f}°C")
        if snap.disk_free_gb < 5 and snap.disk_total_gb > 0:
            self.escalate(Severity.WARNING, "disk", f"Disk space low: {snap.disk_free_gb:.1f} GB free")

    def escalate(self, severity, source, message, details=None):
        event = EscalationEvent(
            timestamp=time.time(),
            severity=severity.value if hasattr(severity, 'value') else severity,
            source=source,
            message=message,
            details=details or {},
        )
        with self._lock:
            self._escalations.append(event)
            if len(self._escalations) > 100:
                self._escalations = self._escalations[-100:]

    def record_task(self, success: bool, gen_time: float) -> None:
        with self._lock:
            if success:
                self._tasks_completed += 1
            else:
                self._tasks_failed += 1
            self._total_gen_time += gen_time

    def get_health_response(self) -> dict:
        with self._lock:
            snap = self._health
        status = "error" if self.bootstrap.phase == "error" else "ok"
        return {
            "status": status,
            "gpu": snap.gpu_name,
            "tts_loaded": _tts_model is not None,
            "ltx_loaded": _ltx_pipe is not None,
            "vram_used_gb": round(snap.vram_used_gb, 2),
            "vram_total_gb": round(snap.vram_total_gb, 2),
            "bootstrap": {
                "phase": self.bootstrap.phase,
                "detail": self.bootstrap.detail,
                "error": self.bootstrap.error,
                "error_category": self.bootstrap.error_category,
            },
            "worker_mode": _worker_mode,
        }


# ---------------------------------------------------------------------------
# Global model handles
# ---------------------------------------------------------------------------
_tts_model = None
_ltx_pipe = None
_active_model: str = ""
_models_dir: str = "/workspace/models"
_output_dir: str = "/workspace/output"
_model_lock = threading.Lock()
_worker_mode: str = "both"
_bootstrap_status = BootstrapStatus()

_DASHSCOPE_API_KEY: str = os.environ.get("DASHSCOPE_API_KEY", "")
_OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")
_QA_BACKEND: str = "dashscope" if _DASHSCOPE_API_KEY else ("openrouter" if _OPENROUTER_API_KEY else "")
_QA_MODEL_DASHSCOPE_STRUCTURAL: str = "qwen-vl-max"
_QA_MODEL_DASHSCOPE_SEMANTIC: str = "qwen-vl-max"
_QA_MODEL_OPENROUTER_STRUCTURAL: str = "google/gemini-2.5-flash-preview"
_QA_MODEL_OPENROUTER_SEMANTIC: str = "google/gemini-2.5-flash-preview"

_MIN_BRIGHTNESS = 20.0
_MIN_CONTRAST = 3.0

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


def _unload_tts():
    global _tts_model, _active_model
    if _tts_model is None:
        return
    del _tts_model
    _tts_model = None
    _active_model = ""
    gc.collect()
    torch.cuda.empty_cache()


def _unload_ltx():
    global _ltx_pipe, _active_model
    if _ltx_pipe is None:
        return
    del _ltx_pipe
    _ltx_pipe = None
    _active_model = ""
    gc.collect()
    torch.cuda.empty_cache()


def _load_tts():
    global _tts_model, _active_model
    if _tts_model is not None:
        return
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
    global _ltx_pipe, _active_model
    if _ltx_pipe is not None:
        return
    if _tts_model is not None:
        _unload_tts()
    from ltx_pipelines.ti2vid_one_stage import TI2VidOneStagePipeline
    from ltx_core.loader.registry import StateDictRegistry
    from ltx_pipelines.utils.model_ledger import ModelLedger

    candidate_ltx23 = os.path.join(_models_dir, "ltx23")
    candidate_ltx2 = os.path.join(_models_dir, "ltx2")
    if os.path.isdir(candidate_ltx23):
        model_path = candidate_ltx23
    elif os.path.isdir(candidate_ltx2):
        model_path = candidate_ltx2
    else:
        model_path = _models_dir

    ckpt_path = os.path.join(model_path, "ltx-2.3-22b-dev.safetensors")
    if not os.path.isfile(ckpt_path):
        raise FileNotFoundError(f"LTX-2.3 checkpoint not found at {ckpt_path}")

    gemma_root = os.path.join(model_path, "gemma")
    if not os.path.isdir(gemma_root):
        raise FileNotFoundError(f"Gemma text encoder not found at {gemma_root}")

    t0 = time.time()
    pipe = TI2VidOneStagePipeline(
        checkpoint_path=ckpt_path,
        gemma_root=gemma_root,
        loras=[],
    )
    registry = StateDictRegistry()
    pipe.model_ledger = ModelLedger(
        dtype=pipe.dtype,
        device=pipe.device,
        checkpoint_path=ckpt_path,
        gemma_root_path=gemma_root,
        loras=[],
        registry=registry,
    )
    _ltx_pipe = pipe
    _active_model = "ltx"
    logger.info("LTX-2.3 loaded in %.1fs", time.time() - t0)


def _generate_tts(text: str, voice: str, language: str) -> tuple[np.ndarray, int]:
    if app.state.vm_agent is not None:
        app.state.vm_agent.record_activity()
    _load_tts()
    profile = _VOICE_PROFILES.get(voice, _VOICE_PROFILES["V1"])
    voice_instruction = profile.get(language, profile.get("en", _VOICE_PROFILES["V1"]["en"]))
    lang_map = {"en": "English", "ru": "Russian"}
    tts_language = lang_map.get(language, "Auto")
    logger.info("VoiceDesign: voice=%s lang=%s", voice, tts_language)
    wavs, sr = _tts_model.generate_voice_design(
        text=text,
        instruct=voice_instruction,
        language=tts_language,
    )
    audio_array = wavs[0] if wavs else np.zeros(sr, dtype=np.float32)
    return audio_array, sr


def _measure_frame_brightness(frames) -> float:
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
    return float(sampled.mean())


def _measure_frame_contrast(frames) -> float:
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


def _frames_to_base64(frames, indices: list[int]) -> list[str]:
    from PIL import Image
    arr = np.asarray(frames)
    result = []
    for idx in indices:
        frame = arr[idx]
        if frame.dtype != np.uint8:
            frame = (np.clip(frame, 0, 1) * 255).astype(np.uint8)
        img = Image.fromarray(frame)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        result.append(b64)
    return result


def _call_vision_model(content_parts: list[dict], model: str, api_url: str, api_key: str) -> dict:
    import httpx
    resp = httpx.post(
        api_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "user", "content": content_parts}], "max_tokens": 400, "temperature": 0.1},
        timeout=90.0,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def _qwen_visual_qa(prompt: str, frames_b64: list[str], visual_style: str = "") -> dict:
    if not _QA_BACKEND:
        raise RuntimeError("OTIO VIOLATION: visual QA unavailable — no API key configured")

    if _QA_BACKEND == "dashscope":
        api_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
        api_key = _DASHSCOPE_API_KEY
        structural_model = _QA_MODEL_DASHSCOPE_STRUCTURAL
        semantic_model = _QA_MODEL_DASHSCOPE_SEMANTIC
    else:
        api_url = "https://openrouter.ai/api/v1/chat/completions"
        api_key = _OPENROUTER_API_KEY
        structural_model = _QA_MODEL_OPENROUTER_STRUCTURAL
        semantic_model = _QA_MODEL_OPENROUTER_SEMANTIC

    image_parts = []
    for b64 in frames_b64:
        image_parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    structural_parts = list(image_parts)
    structural_parts.append({
        "type": "text",
        "text": (
            "You are a video output integrity checker. Check for corruption, AI wonk, body horror. "
            "Respond EXACTLY: {\"verdict\": \"rejected|pass\", \"defects\": \"description\"}"
        ),
    })

    try:
        p1 = _call_vision_model(structural_parts, structural_model, api_url, api_key)
        if p1.get("verdict", "pass").lower() == "rejected":
            return {"quality": "rejected", "qa_reason": f"STRUCTURAL: {p1.get('defects', '')}", "qa_pass": "structural"}
    except Exception as e:
        logger.error("QA Pass 1 failed: %s", e)

    style_section = f"\nMOVIE STYLE:\n{visual_style}\n" if visual_style else ""
    semantic_parts = list(image_parts)
    semantic_parts.append({
        "type": "text",
        "text": (
            f"Evaluate this video for prompt adherence and quality.\n"
            f'Prompt: "{prompt}"{style_section}'
            f'Respond EXACTLY: {{"quality": "rejected|poor|good|excellent", "qa_reason": "brief description"}}'
        ),
    })

    try:
        p2 = _call_vision_model(semantic_parts, semantic_model, api_url, api_key)
        quality = p2.get("quality", "unknown").lower()
        if quality not in ("rejected", "poor", "good", "excellent"):
            quality = "unknown"
        return {"quality": quality, "qa_reason": p2.get("qa_reason", ""), "qa_pass": "semantic"}
    except Exception as e:
        logger.error("QA Pass 2 failed: %s", e)
        return {"quality": "poor", "qa_reason": f"QA failed: {e}", "qa_pass": "error"}


@torch.inference_mode()
def _ltx_generate_once(
    prompt: str, negative_prompt: str, width: int, height: int,
    num_frames: int, fps: float, seed: int, num_inference_steps: int,
    guidance_scale: float, stg_scale: float, modality_scale: float,
    guidance_rescale: float, stg_blocks: list[int],
) -> np.ndarray:
    from ltx_core.components.guiders import MultiModalGuiderParams
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()

    if torch.cuda.is_available():
        _alloc = torch.cuda.memory_allocated() / 1e9
        _resv = torch.cuda.memory_reserved() / 1e9
        _total = torch.cuda.get_device_properties(0).total_memory / 1e9
        logger.info("VRAM before pipeline: alloc=%.2fGB resv=%.2fGB total=%.2fGB free=%.2fGB", _alloc, _resv, _total, _total - _resv)

    video_guider = MultiModalGuiderParams(
        cfg_scale=guidance_scale, stg_scale=stg_scale, rescale_scale=guidance_rescale,
        modality_scale=modality_scale, skip_step=0, stg_blocks=stg_blocks,
    )
    audio_guider = MultiModalGuiderParams(
        cfg_scale=7.0, stg_scale=stg_scale, rescale_scale=guidance_rescale,
        modality_scale=modality_scale, skip_step=0, stg_blocks=stg_blocks,
    )

    try:
        video_iter, _audio = _ltx_pipe(
            prompt=prompt, negative_prompt=negative_prompt, seed=seed,
            height=height, width=width, num_frames=num_frames, frame_rate=fps,
            num_inference_steps=num_inference_steps,
            video_guider_params=video_guider, audio_guider_params=audio_guider, images=[],
        )
        chunks = []
        for chunk in video_iter:
            chunks.append(chunk.cpu().numpy())
        candidate_frames = np.concatenate(chunks, axis=0)
    except Exception as exc:
        logger.error("LTX generation failed: %s", exc)
        gc.collect()
        torch.cuda.empty_cache()
        raise
    finally:
        gc.collect()
        torch.cuda.empty_cache()
        if torch.cuda.is_available():
            _alloc = torch.cuda.memory_allocated() / 1e9
            _resv = torch.cuda.memory_reserved() / 1e9
            logger.info("VRAM after pipeline: alloc=%.2fGB resv=%.2fGB", _alloc, _resv)

    return candidate_frames


def _generate_video(
    prompt: str, duration_sec: float, width: int, height: int,
    num_frames: int | None, seed: int, num_inference_steps: int,
    guidance_scale: float, negative_prompt: str = "", visual_style: str = "",
    stg_scale: float = 1.0, modality_scale: float = 3.0,
    guidance_rescale: float = 0.7, stg_blocks: list[int] | None = None,
) -> tuple[bytes, dict]:
    if app.state.vm_agent is not None:
        app.state.vm_agent.record_activity()
    qa_available = bool(_QA_BACKEND)
    _load_ltx()

    fps = 24
    if num_frames is None:
        raw_frames = int(duration_sec * fps)
        num_frames = max(9, ((raw_frames - 1) // 8) * 8 + 1)

    baseline_negatives = "worst quality, inconsistent motion, blurry, jittery, distorted, static, low resolution, morphing, warping, flicker, text, watermark, logo"
    negative_prompt = f"{negative_prompt}, {baseline_negatives}" if negative_prompt else baseline_negatives

    logger.info("Generating video: %d frames (%.1fs), %dx%d, seed=%d", num_frames, num_frames / fps, width, height, seed)
    t0 = time.time()

    _stg_blocks = stg_blocks if stg_blocks is not None else [28]
    max_attempts = 3
    current_seed = seed
    best_passing_frames = None
    best_passing_score = -1.0
    best_passing_seed = seed
    best_passing_qa: dict = {"quality": "unknown", "qa_reason": "Not evaluated"}
    best_failing_frames = None
    best_failing_score = -1.0
    best_failing_seed = seed
    final_attempt = 0

    for attempt in range(1, max_attempts + 1):
        final_attempt = attempt
        gc.collect()
        torch.cuda.empty_cache()

        candidate_frames = _ltx_generate_once(
            prompt=prompt, negative_prompt=negative_prompt, width=width, height=height,
            num_frames=num_frames, fps=fps, seed=current_seed, num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale, stg_scale=stg_scale, modality_scale=modality_scale,
            guidance_rescale=guidance_rescale, stg_blocks=_stg_blocks,
        )

        if np.isnan(candidate_frames).any() or np.isinf(candidate_frames).any():
            nan_frac = float(np.isnan(candidate_frames).mean())
            logger.warning("NaN/Inf in decoded frames (%.1f%%)", nan_frac * 100)
            candidate_frames = np.nan_to_num(candidate_frames, nan=0, posinf=255, neginf=0).astype(np.uint8)

        brightness = _measure_frame_brightness(candidate_frames)
        contrast = _measure_frame_contrast(candidate_frames)
        score = brightness + contrast
        logger.info("Brightness check: brightness=%.1f/255, contrast=%.1f, seed=%d", brightness, contrast, current_seed)

        if brightness < _MIN_BRIGHTNESS or contrast < _MIN_CONTRAST:
            if score > best_failing_score:
                best_failing_frames = candidate_frames
                best_failing_score = score
                best_failing_seed = current_seed
            if attempt < max_attempts:
                current_seed = (current_seed + 7919) % (2**31)
                continue
            else:
                break

        is_new_best = score > best_passing_score

        if qa_available:
            n = candidate_frames.shape[0]
            sample_indices = [0, n // 2, n - 1] if n >= 3 else list(range(n))
            frames_b64 = _frames_to_base64(candidate_frames, sample_indices)
            qa_result = _qwen_visual_qa(prompt, frames_b64, visual_style=visual_style)

            logger.info("Qwen QA: quality=%s", qa_result["quality"])

            if qa_result["quality"] == "rejected":
                if best_passing_qa.get("quality") not in ("poor", "good", "excellent"):
                    best_passing_frames = candidate_frames
                    best_passing_seed = current_seed
                    best_passing_qa = qa_result
                break

            if qa_result["quality"] in ("good", "excellent"):
                best_passing_frames = candidate_frames
                best_passing_score = score
                best_passing_seed = current_seed
                best_passing_qa = qa_result
                break

            if is_new_best:
                best_passing_frames = candidate_frames
                best_passing_score = score
                best_passing_seed = current_seed
                best_passing_qa = qa_result
            if attempt < max_attempts:
                current_seed = (current_seed + 7919) % (2**31)
        else:
            logger.warning("QA skipped: no API key")
            best_passing_frames = candidate_frames
            best_passing_score = score
            best_passing_seed = current_seed
            best_passing_qa = {"quality": "unknown", "qa_reason": "QA skipped: no API key configured"}
            break

    if qa_available and best_passing_qa.get("quality") == "unknown":
        best_passing_qa["quality"] = "poor"
        best_passing_qa["qa_reason"] = f"Fail-closed: QA could not evaluate after {max_attempts} attempts."

    if best_passing_frames is not None:
        video_frames = best_passing_frames
        best_seed = best_passing_seed
        best_qa = best_passing_qa
    else:
        video_frames = best_failing_frames
        best_seed = best_failing_seed
        best_qa = {"quality": "poor", "qa_reason": "All attempts failed brightness check"}

    elapsed = time.time() - t0
    logger.info("Video generated in %.1fs (seed=%d, qa=%s)", elapsed, best_seed, best_qa.get("quality", "unknown"))

    output_path = os.path.join(_output_dir, f"clip_{uuid.uuid4().hex[:8]}.mp4")
    os.makedirs(_output_dir, exist_ok=True)

    _ffmpeg_write = subprocess.run(
        [
            "ffmpeg", "-y", "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{video_frames.shape[2]}x{video_frames.shape[1]}",
            "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            output_path,
        ],
        input=video_frames.tobytes(), capture_output=True, timeout=120,
    )
    if _ffmpeg_write.returncode != 0:
        stderr_msg = (_ffmpeg_write.stderr or b"")[:500]
        raise RuntimeError(f"ffmpeg encode failed: {stderr_msg}")

    with open(output_path, "rb") as f:
        data = f.read()

    try:
        os.unlink(output_path)
    except OSError:
        pass

    qa_status = {
        "quality": best_qa.get("quality", "unknown"),
        "qa_reason": best_qa.get("qa_reason", ""),
        "attempts": final_attempt,
        "seed": best_seed,
        "brightness": round(_measure_frame_brightness(video_frames), 1),
        "contrast": round(_measure_frame_contrast(video_frames), 1),
    }

    return data, qa_status


# ---------------------------------------------------------------------------
# API endpoints — ONLY GET / and POST /
# ---------------------------------------------------------------------------

@app.get("/")
async def get_endpoint():
    """Probe. Returns plain text status."""
    if app.state.vm_agent is not None:
        app.state.vm_agent.record_activity()

    gpu_name = "unknown"
    vram_used = 0.0
    vram_total = 0.0
    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)
        vram_used = torch.cuda.memory_allocated(0) / 1e9
        vram_total = torch.cuda.get_device_properties(0).total_memory / 1e9

    status = "error" if _bootstrap_status.phase == "error" else "ok"
    tts = "yes" if _tts_model is not None else "no"
    ltx = "yes" if _ltx_pipe is not None else "no"
    return Response(
        content=f"{status} {gpu_name} tts={tts} ltx={ltx} vram={vram_used:.1f}/{vram_total:.1f}GB mode={_worker_mode}",
        media_type="text/plain",
    )


@app.post("/")
async def post_endpoint(request: Request):
    """Send text to the worker. The worker decides what to do based on mode.

    No structure enforced. The text is just text.
    """
    body = await request.body()
    text = body.decode("utf-8").strip()

    if app.state.vm_agent is not None:
        app.state.vm_agent.record_activity()

    if _worker_mode == "tts":
        t0 = time.time()
        success = False
        with _model_lock:
            try:
                audio_array, sr = _generate_tts(text, "V1", "en")
                success = True
            except Exception as e:
                logger.error("TTS failed: %s", e, exc_info=True)
                if app.state.vm_agent:
                    app.state.vm_agent.record_task(False, time.time() - t0)
                return Response(content=f"TTS failed: {e}", media_type="text/plain", status_code=500)

        buf = io.BytesIO()
        sf.write(buf, audio_array, sr, format="WAV", subtype="PCM_16")
        wav_bytes = buf.getvalue()

        elapsed = time.time() - t0
        duration = len(audio_array) / sr
        if app.state.vm_agent:
            app.state.vm_agent.record_task(success, elapsed)
        logger.info("TTS done: %.1fs audio in %.1fs", duration, elapsed)

        return Response(
            content=wav_bytes,
            media_type="audio/wav",
            headers={
                "X-Audio-Duration": str(round(duration, 3)),
                "X-Sample-Rate": str(sr),
                "X-Gen-Time": str(round(elapsed, 3)),
            },
        )

    elif _worker_mode == "ltx":
        t0 = time.time()
        try:
            mp4_bytes, qa_status = _generate_video(
                prompt=text, duration_sec=5.0, width=512, height=320,
                num_frames=None, seed=42, num_inference_steps=30,
                guidance_scale=3.0, negative_prompt="", visual_style="",
                stg_scale=1.0, modality_scale=3.0, guidance_rescale=0.7,
                stg_blocks=[28],
            )
        except Exception as e:
            logger.error("Video failed: %s", e, exc_info=True)
            if app.state.vm_agent:
                app.state.vm_agent.record_task(False, time.time() - t0)
            return Response(content=f"Video failed: {e}", media_type="text/plain", status_code=500)

        elapsed = time.time() - t0
        if app.state.vm_agent:
            app.state.vm_agent.record_task(True, elapsed)
        logger.info("Video done: %d bytes in %.1fs, qa=%s", len(mp4_bytes), elapsed, qa_status.get("quality", "unknown"))

        return Response(
            content=mp4_bytes,
            media_type="video/mp4",
            headers={
                "X-Gen-Time": str(round(elapsed, 3)),
                "X-File-Size": str(len(mp4_bytes)),
                "X-QA-Quality": qa_status.get("quality", "unknown"),
                "X-QA-Reason": base64.b64encode(qa_status.get("qa_reason", "").encode("utf-8")).decode("ascii"),
                "X-QA-Attempts": str(qa_status.get("attempts", 1)),
                "X-QA-Seed": str(qa_status.get("seed", 42)),
            },
        )

    else:
        return Response(content=f"Unknown mode: {_worker_mode}", media_type="text/plain", status_code=503)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GPU Worker")
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--models-dir", type=str, default="/workspace/models")
    parser.add_argument("--output-dir", type=str, default="/workspace/output")
    parser.add_argument("--mode", type=str, default="both", choices=["tts", "ltx", "both"])
    args = parser.parse_args()

    global _models_dir, _output_dir, _worker_mode, _bootstrap_status
    _models_dir = args.models_dir
    _output_dir = args.output_dir
    _worker_mode = args.mode
    os.makedirs(_output_dir, exist_ok=True)

    agent = VMAgent(worker_mode=_worker_mode, models_dir=_models_dir, output_dir=_output_dir)
    app.state.vm_agent = agent
    _bootstrap_status = agent.bootstrap

    logger.info("Starting VM Agent on %s:%d (mode=%s)", args.host, args.port, _worker_mode)

    def _agent_lifecycle():
        ok = agent.run_bootstrap()
        if ok:
            agent.start_monitoring()
        else:
            agent.start_monitoring()
            logger.error("VMAgent: bootstrap failed")

    threading.Thread(target=_agent_lifecycle, name="vm-agent-lifecycle", daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
