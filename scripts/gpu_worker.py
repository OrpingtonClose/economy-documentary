#!/usr/bin/env python3
"""GPU Worker — autonomous VM agent for TTS and video generation.

Each VM runs its own agent (VMAgent) that manages the full lifecycle:
    1. Bootstrap — install deps, download models, categorise errors
    2. Model loading — load into VRAM with retry/recovery
    3. Self-monitoring — GPU health, disk, memory on a continuous loop
    4. Work execution — TTS/video generation via HTTP endpoints
    5. Escalation — structured escalation log read by the central overseer

The central process (server/infra_agent.py) is the *tending overseer*:
it reads /status from each VM agent, processes escalations, and makes
lifecycle decisions (reprovision, restart, etc.).  It does NOT duplicate
the monitoring that the VM agent already performs.

Usage:
    python gpu_worker.py --mode tts  [--port 8880] [--models-dir ...]  # TTS-only VM
    python gpu_worker.py --mode ltx  [--port 8880] [--models-dir ...]  # LTX-only VM
    python gpu_worker.py --mode both [--port 8880] [--models-dir ...]  # shared (legacy)

Models are expected at:
    {models_dir}/qwen3-tts-voicedesign/  — Qwen3-TTS-12Hz-1.7B-VoiceDesign
    {models_dir}/ltx2/                   — LTX-2.3 directory:
        ltx-2.3-22b-dev.safetensors      — Official Lightricks LTX-2.3 single-file checkpoint
        gemma/                          — Gemma-3 1B text encoder weights

The bootstrap script (gpu_bootstrap.sh) downloads these from B2/HuggingFace.
Uses the official Lightricks ltx-pipelines package for inference.
bf16 only — no FP8, no quantization.
"""
from __future__ import annotations

import os

# ---- CUDA memory allocator config (MUST be set before importing torch) ----
# The 22B LTX-2.3 transformer weighs ~46GB at bf16.  The pipeline uses a
# block-based lifecycle (text encoder → transformer → VAE) so only one
# major component is in VRAM at a time.  PyTorch's default allocator fragments reserved
# memory into small non-contiguous blocks that can't satisfy even 32MB
# allocations.  ``expandable_segments`` lets the allocator grow and reuse
# reserved memory efficiently, reclaiming the ~874MB of reserved-but-
# unallocated memory that would otherwise be wasted.
os.environ.setdefault(
    "PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True"
)

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

# Ensure NVIDIA CUDA runtime libs (libcudart.so.13) are preloaded.
# PyTorch cu130 doesn't bundle libcudart — it's in the nvidia-cuda-runtime
# pip package at nvidia/cu13/lib/.  We preload it via ctypes RTLD_GLOBAL
# so ltx-core compiled extensions can find it.
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
    # Also check user site-packages
    _usp = _site.getusersitepackages()
    if isinstance(_usp, str):
        _nv = os.path.join(_usp, "nvidia")
        if os.path.isdir(_nv):
            for _sub in os.listdir(_nv):
                _lib = os.path.join(_nv, _sub, "lib")
                if os.path.isdir(_lib):
                    _search_dirs.append(_lib)
    # Set LD_LIBRARY_PATH for subprocesses
    if _search_dirs:
        _ld = os.environ.get("LD_LIBRARY_PATH", "")
        _new_paths = ":".join(d for d in _search_dirs if d not in _ld)
        if _new_paths:
            os.environ["LD_LIBRARY_PATH"] = f"{_new_paths}:{_ld}" if _ld else _new_paths
    # Preload libcudart via ctypes so it's available for dlopen()
    for _d in _search_dirs:
        for _so in sorted(_glob.glob(os.path.join(_d, "libcudart*.so*"))):
            try:
                _ctypes.CDLL(_so, mode=_ctypes.RTLD_GLOBAL)
            except OSError:
                pass
    del _search_dirs, _site
except Exception:
    pass
del _ctypes, _glob

import numpy as np
import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

# No monkey-patches needed — using official ltx-pipelines, not diffusers.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("gpu_worker")

app = FastAPI(title="Documentary GPU Worker")

# ---------------------------------------------------------------------------
# Bootstrap status — defined early so it's available as a global before
# the Pydantic request/response models section.
# ---------------------------------------------------------------------------

class BootstrapStatus(BaseModel):
    """Structured bootstrap status — reported in /health so the provisioner
    can see WHY a worker isn't ready, not just that it isn't."""
    phase: str = "idle"           # idle | deps | models | loading | ready | error
    detail: str = ""              # human-readable description of current activity
    error: str = ""               # non-empty only when phase == "error"
    error_category: str = ""      # "auth", "network", "disk", "missing_file", "runtime"
    started_at: float = 0.0
    completed_at: float = 0.0


# ---------------------------------------------------------------------------
# VMAgent — autonomous infrastructure agent that runs ON this VM.
#
# Manages the full lifecycle: bootstrap, model loading, self-monitoring,
# recovery, and escalation.  The central overseer reads /status to see
# what this agent is doing and processes its escalation log.
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class EscalationEvent:
    """A single escalation event for the overseer to read."""
    timestamp: float
    severity: str          # "info" | "warning" | "critical"
    source: str            # e.g. "bootstrap", "gpu", "disk", "generation"
    message: str
    details: dict = field(default_factory=dict)
    acked: bool = False    # True once the overseer has acknowledged it

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
    """Point-in-time self-monitoring snapshot."""
    gpu_name: str = ""
    vram_used_gb: float = 0.0
    vram_total_gb: float = 0.0
    vram_pct: float = 0.0
    gpu_temp_c: float = 0.0
    disk_free_gb: float = 0.0
    disk_total_gb: float = 0.0
    disk_pct: float = 0.0
    checked_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "gpu_name": self.gpu_name,
            "vram_used_gb": round(self.vram_used_gb, 2),
            "vram_total_gb": round(self.vram_total_gb, 2),
            "vram_pct": round(self.vram_pct, 1),
            "gpu_temp_c": round(self.gpu_temp_c, 1),
            "disk_free_gb": round(self.disk_free_gb, 1),
            "disk_total_gb": round(self.disk_total_gb, 1),
            "disk_pct": round(self.disk_pct, 1),
            "checked_at": self.checked_at,
        }


class VMAgent:
    """Autonomous infrastructure agent that runs on this VM.

    Responsibilities:
    1. Bootstrap lifecycle — run bootstrap script, categorise errors, retry
       transient failures (network), escalate permanent ones.
    2. Model loading — load models into VRAM with retry on OOM.
    3. Self-monitoring — continuous background loop checking GPU health,
       disk space, VRAM pressure, temperature.
    4. Local recovery — when self-monitoring detects issues (VRAM > 95%,
       disk < 5GB, temp > 85°C), take local action (log, escalate).
    5. Escalation — maintain a structured escalation log that the central
       overseer reads via /status and /escalations endpoints.
    6. Task tracking — count generations completed, failures, avg latency.

    The VMAgent is a singleton — created once at startup and referenced
    by FastAPI endpoints.
    """

    def __init__(
        self,
        worker_mode: str,
        models_dir: str,
        output_dir: str,
        monitor_interval: float = 30.0,
    ) -> None:
        self.worker_mode = worker_mode
        self.models_dir = models_dir
        self.output_dir = output_dir
        self._monitor_interval = monitor_interval

        # Bootstrap status
        self.bootstrap = BootstrapStatus()

        # Self-monitoring
        self._health: HealthSnapshot = HealthSnapshot()
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

        # Escalation log
        self._escalations: list[EscalationEvent] = []

        # Task tracking
        self._tasks_completed: int = 0
        self._tasks_failed: int = 0
        self._total_gen_time: float = 0.0

    # ------------------------------------------------------------------
    # Bootstrap lifecycle
    # ------------------------------------------------------------------

    def run_bootstrap(self) -> bool:
        """Run the full bootstrap lifecycle: deps → models → load.

        Returns True if bootstrap succeeded, False on failure.
        Retries transient (network) errors up to 2 times.
        """
        self.bootstrap.started_at = time.time()

        # Phase 1: Check if models are already present
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

        # Phase 2: Load models into VRAM
        ok = self._load_models()
        if not ok:
            return False

        self.bootstrap.phase = "ready"
        self.bootstrap.detail = "All models loaded"
        self.bootstrap.completed_at = time.time()
        elapsed = self.bootstrap.completed_at - self.bootstrap.started_at
        logger.info("VMAgent: bootstrap + model loading complete in %.1fs", elapsed)
        return True

    def _run_bootstrap_script(self) -> bool:
        """Execute gpu_bootstrap.sh with retry on transient failures."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        bootstrap_script = os.path.join(script_dir, "gpu_bootstrap.sh")

        if not os.path.isfile(bootstrap_script):
            self._fail_bootstrap(
                f"Bootstrap script not found at {bootstrap_script}",
                "missing_file",
            )
            return False

        max_retries = 3  # total attempts (1 initial + 2 retries)
        for attempt in range(1, max_retries + 1):
            self.bootstrap.phase = "deps" if attempt == 1 else "models"
            self.bootstrap.detail = (
                f"Running bootstrap (attempt {attempt}/{max_retries}): "
                "installing dependencies and downloading models"
            )
            logger.info(
                "VMAgent: bootstrap attempt %d/%d", attempt, max_retries
            )

            try:
                env = os.environ.copy()
                env["WORKER_MODE"] = self.worker_mode
                result = subprocess.run(
                    ["bash", bootstrap_script],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=3600,
                )
                if result.returncode == 0:
                    logger.info("VMAgent: bootstrap succeeded on attempt %d", attempt)
                    return True

                stderr = result.stderr[-2000:] if result.stderr else ""
                stdout_tail = result.stdout[-1000:] if result.stdout else ""
                error_cat = self._categorise_error(stderr + stdout_tail)

                # Only retry transient (network) errors
                if error_cat == "network" and attempt < max_retries:
                    wait = 15 * attempt
                    self.escalate(
                        Severity.WARNING, "bootstrap",
                        f"Bootstrap attempt {attempt} failed (network) — "
                        f"retrying in {wait}s: {stderr[-200:]}",
                    )
                    time.sleep(wait)
                    continue

                # Permanent failure
                self._fail_bootstrap(
                    f"Bootstrap failed (rc={result.returncode}, attempt {attempt}): "
                    f"{stderr[-500:]}",
                    error_cat,
                )
                return False

            except subprocess.TimeoutExpired:
                if attempt < max_retries:
                    self.escalate(
                        Severity.WARNING, "bootstrap",
                        f"Bootstrap timed out on attempt {attempt} — retrying",
                    )
                    continue
                self._fail_bootstrap("Bootstrap timed out after 3600s", "network")
                return False
            except Exception as e:
                self._fail_bootstrap(f"Bootstrap exception: {e}", "runtime")
                return False

        # Should not reach here
        self._fail_bootstrap("Bootstrap exhausted all retries", "runtime")
        return False

    def _load_models(self) -> bool:
        """Load models into VRAM."""
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
        """Record a bootstrap failure and escalate."""
        self.bootstrap.phase = "error"
        self.bootstrap.error = error
        self.bootstrap.error_category = category
        logger.error("VMAgent BOOTSTRAP FAILED (%s): %s", category, error)
        self.escalate(Severity.CRITICAL, "bootstrap", error, {"category": category})

    @staticmethod
    def _categorise_error(combined: str) -> str:
        """Categorise an error from bootstrap output."""
        if "401" in combined or "403" in combined or "GatedRepo" in combined:
            return "auth"
        if "404" in combined or "EntryNotFound" in combined:
            return "missing_file"
        if "No space left" in combined or "disk" in combined.lower():
            return "disk"
        if "ConnectionError" in combined or "timeout" in combined.lower():
            return "network"
        return "runtime"

    # ------------------------------------------------------------------
    # Self-monitoring loop
    # ------------------------------------------------------------------

    def start_monitoring(self) -> None:
        """Launch the self-monitoring daemon thread."""
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._shutdown.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="vm-agent-monitor",
            daemon=True,
        )
        self._monitor_thread.start()
        logger.info("VMAgent: self-monitoring started (interval=%.0fs)", self._monitor_interval)

    def stop_monitoring(self) -> None:
        """Signal the monitoring loop to stop."""
        self._shutdown.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5.0)

    def _monitor_loop(self) -> None:
        """Continuous self-monitoring: GPU, disk, VRAM, temperature."""
        # Run one immediate check
        self._check_health()

        while not self._shutdown.is_set():
            if self._shutdown.wait(timeout=self._monitor_interval):
                break
            self._check_health()

        logger.info("VMAgent: self-monitoring stopped")

    def _check_health(self) -> None:
        """Run all self-monitoring checks and escalate if needed."""
        snap = HealthSnapshot(checked_at=time.time())

        # GPU / VRAM
        if torch.cuda.is_available():
            snap.gpu_name = torch.cuda.get_device_name(0)
            snap.vram_used_gb = torch.cuda.memory_allocated(0) / 1e9
            snap.vram_total_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
            snap.vram_pct = (
                snap.vram_used_gb / max(snap.vram_total_gb, 0.01) * 100
            )

        # GPU temperature via nvidia-smi
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                snap.gpu_temp_c = float(result.stdout.strip().split("\n")[0])
        except Exception:
            pass

        # Disk space
        try:
            usage = shutil.disk_usage(self.models_dir)
            snap.disk_free_gb = usage.free / (1024**3)
            snap.disk_total_gb = usage.total / (1024**3)
            snap.disk_pct = usage.used / max(usage.total, 1) * 100
        except Exception:
            pass

        with self._lock:
            self._health = snap

        # Escalate on concerning conditions
        if snap.vram_pct > 95:
            self.escalate(
                Severity.WARNING, "gpu",
                f"VRAM pressure: {snap.vram_pct:.0f}% used "
                f"({snap.vram_used_gb:.1f}/{snap.vram_total_gb:.1f} GB)",
            )
        if snap.gpu_temp_c > 85:
            self.escalate(
                Severity.WARNING, "gpu",
                f"GPU temperature high: {snap.gpu_temp_c:.0f}°C",
            )
        if snap.disk_free_gb < 5 and snap.disk_total_gb > 0:
            self.escalate(
                Severity.WARNING, "disk",
                f"Disk space low: {snap.disk_free_gb:.1f} GB free",
            )

    # ------------------------------------------------------------------
    # Escalation
    # ------------------------------------------------------------------

    def escalate(
        self,
        severity: Severity,
        source: str,
        message: str,
        details: Optional[dict] = None,
    ) -> None:
        """Add a structured escalation event.

        The central overseer reads these via /escalations.
        """
        event = EscalationEvent(
            timestamp=time.time(),
            severity=severity.value,
            source=source,
            message=message,
            details=details or {},
        )
        with self._lock:
            self._escalations.append(event)
            # Keep last 100 to prevent unbounded growth
            if len(self._escalations) > 100:
                self._escalations = self._escalations[-100:]

        log_fn = {
            Severity.INFO: logger.info,
            Severity.WARNING: logger.warning,
            Severity.CRITICAL: logger.critical,
        }.get(severity, logger.warning)
        log_fn("VMAgent ESCALATION [%s] %s: %s", severity.value, source, message)

    def ack_escalations(self, before_ts: float) -> int:
        """Mark escalations before `before_ts` as acknowledged.

        Returns number of newly acknowledged events.
        Called by the overseer via POST /escalations/ack.
        """
        count = 0
        with self._lock:
            for e in self._escalations:
                if not e.acked and e.timestamp <= before_ts:
                    e.acked = True
                    count += 1
        return count

    # ------------------------------------------------------------------
    # Task tracking
    # ------------------------------------------------------------------

    def record_task(self, success: bool, gen_time: float) -> None:
        """Record a completed generation task."""
        with self._lock:
            if success:
                self._tasks_completed += 1
            else:
                self._tasks_failed += 1
            self._total_gen_time += gen_time

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Full status snapshot for the overseer.

        This is the single source of truth for what this VM is doing.
        The overseer reads this via GET /status.
        """
        with self._lock:
            health = self._health.to_dict()
            escalations = [e.to_dict() for e in self._escalations[-20:]]
            unacked = sum(1 for e in self._escalations if not e.acked)
            tasks_completed = self._tasks_completed
            tasks_failed = self._tasks_failed
            avg_gen = (
                self._total_gen_time / max(tasks_completed + tasks_failed, 1)
            )

        return {
            "worker_mode": self.worker_mode,
            "bootstrap": {
                "phase": self.bootstrap.phase,
                "detail": self.bootstrap.detail,
                "error": self.bootstrap.error,
                "error_category": self.bootstrap.error_category,
                "started_at": self.bootstrap.started_at,
                "completed_at": self.bootstrap.completed_at,
            },
            "models": {
                "tts_loaded": _tts_model is not None,
                "ltx_loaded": _ltx_pipe is not None,
            },
            "health": health,
            "escalations": {
                "recent": escalations,
                "unacked_count": unacked,
                "total": len(self._escalations),
            },
            "tasks": {
                "completed": tasks_completed,
                "failed": tasks_failed,
                "avg_gen_time_sec": round(avg_gen, 1),
            },
        }

    def get_health_response(self) -> dict:
        """Quick health check for backward compat with /health endpoint."""
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
                "started_at": self.bootstrap.started_at,
                "completed_at": self.bootstrap.completed_at,
            },
            # GAP 5.1: Model identity
            "tts_model_name": "Qwen3-TTS" if _tts_model is not None else "",
            "ltx_model_path": str(_models_dir) + "/ltx2" if _ltx_pipe is not None else "",
            "worker_mode": _worker_mode,
        }


# Module-level singleton — set in main()
_vm_agent: Optional[VMAgent] = None


# ---------------------------------------------------------------------------
# Global model handles — model swapping for VRAM management
# ---------------------------------------------------------------------------
_tts_model = None  # Qwen3TTSModel instance
_ltx_pipe = None  # TI2VidOneStagePipeline or TI2VidTwoStagesPipeline
_active_model: str = ""  # "tts" or "ltx" — tracks which model is on GPU
_models_dir: str = "/workspace/models"
_output_dir: str = "/workspace/output"
_model_lock = threading.Lock()  # Serialise all model load/unload/inference
_worker_mode: str = "both"  # "tts", "ltx", or "both"
_bootstrap_status = BootstrapStatus()  # structured bootstrap status

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
    # The pipeline uses a block-based lifecycle: text encoder → transformer
    # → VAE decoder, loading/unloading each sequentially.  The ~46GB
    # transformer fits on 80GB GPUs with headroom for activations.
    # 512x320 generates comfortably on A100-80GB.
    width: int = 512
    height: int = 320
    num_frames: int | None = None  # auto-calculated from duration if None
    seed: int = 42
    # LTX-2.3 official parameters (from dg845/LTX-2.3-Diffusers example):
    num_inference_steps: int = 30  # LTX-2.3 dev: 30 steps
    guidance_scale: float = 3.0  # LTX-2.3 dev: CFG=3.0
    stg_scale: float = 1.0  # spatio-temporal guidance scale
    modality_scale: float = 3.0  # modality (video vs audio) guidance
    guidance_rescale: float = 0.7  # guidance rescale factor
    stg_blocks: list[int] = [28]  # spatio-temporal guidance block indices


class HealthResponse(BaseModel):
    status: str
    gpu: str
    tts_loaded: bool
    ltx_loaded: bool
    vram_used_gb: float
    vram_total_gb: float
    bootstrap: BootstrapStatus | None = None
    # GAP 5.1: Model identity fields — report WHICH models are loaded,
    # not just boolean loaded/not-loaded.
    tts_model_name: str = ""
    ltx_model_path: str = ""
    worker_mode: str = ""  # "tts", "ltx", or "both"


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
    """Load LTX-2.3 pipeline using official Lightricks ltx-pipelines package.

    Uses the single-file checkpoint (ltx-2.3-22b-dev.safetensors) with
    TI2VidOneStagePipeline — no diffusers, no upscalers, no LoRAs.

    The pipeline uses a block-based lifecycle: each component (text encoder,
    transformer, VAE decoder) is built on demand and freed after use.
    Each builder's sd_ops filter tensors at the safetensors level (only
    loading keys matching the component's prefix), so the full 46GB file
    is never loaded wholesale.  Peak VRAM is dominated by the ~44GB
    transformer alone, not the sum of all components.

    We use StateDictRegistry (instead of the default DummyRegistry) so
    that state dicts are loaded to **CPU** first and then moved to GPU
    per-component.  This prevents transient GPU spikes from the loader
    and enables cross-builder caching when the same checkpoint is read
    for different components.

    Always unloads TTS first if loaded — prevents OOM from both models
    coexisting in VRAM.  In single-mode (--mode ltx) TTS should never be
    loaded, so the guard is a safety net rather than normal path.
    """
    global _ltx_pipe, _active_model
    if _ltx_pipe is not None:
        return

    # Always free VRAM if TTS is loaded — OOM safety net
    if _tts_model is not None:
        _unload_tts()

    from ltx_pipelines.ti2vid_one_stage import TI2VidOneStagePipeline
    from ltx_core.loader.registry import StateDictRegistry
    from ltx_pipelines.utils.model_ledger import ModelLedger

    # Locate model directory
    candidate_ltx23 = os.path.join(_models_dir, "ltx23")
    candidate_ltx2 = os.path.join(_models_dir, "ltx2")
    if os.path.isdir(candidate_ltx23):
        model_path = candidate_ltx23
    elif os.path.isdir(candidate_ltx2):
        model_path = candidate_ltx2
    else:
        model_path = _models_dir

    # Find the single-file checkpoint (LTX-2.3 22B dev)
    ckpt_path = os.path.join(model_path, "ltx-2.3-22b-dev.safetensors")
    if not os.path.isfile(ckpt_path):
        raise FileNotFoundError(
            f"LTX-2.3 checkpoint not found at {ckpt_path}. "
            "Run gpu_bootstrap.sh to download model files."
        )

    # Find Gemma text encoder root
    gemma_root = os.path.join(model_path, "gemma")
    if not os.path.isdir(gemma_root):
        raise FileNotFoundError(
            f"Gemma text encoder not found at {gemma_root}. "
            "Run gpu_bootstrap.sh to download model files."
        )

    t0 = time.time()

    logger.info(
        "Loading LTX-2.3 one-stage pipeline from %s ...", model_path
    )
    pipe = TI2VidOneStagePipeline(
        checkpoint_path=ckpt_path,
        gemma_root=gemma_root,
        loras=[],
    )

    # Replace the default DummyRegistry with StateDictRegistry so that
    # checkpoint tensors are loaded to CPU first, then moved to GPU
    # per-component.  This avoids loading the full 46GB state dict
    # directly onto GPU (which with DummyRegistry causes transient
    # spikes that combine with autograd graphs to OOM).
    registry = StateDictRegistry()
    pipe.model_ledger = ModelLedger(
        dtype=pipe.dtype,
        device=pipe.device,
        checkpoint_path=ckpt_path,
        gemma_root_path=gemma_root,
        loras=[],
        registry=registry,
    )
    logger.info(
        "One-stage pipeline created (StateDictRegistry → CPU-side caching)."
    )

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
# Two-pass QA model ensemble:
#   Pass 1 (structural integrity): fast check for corruption, grid artifacts,
#           body horror, overt AI wonk.  Uses a strong vision model.
#   Pass 2 (semantic quality): prompt adherence, style conformance, artistic merit.
#           Uses the main VL model.
# If Pass 1 returns REJECTED, Pass 2 is skipped entirely.
_QA_MODEL_DASHSCOPE_STRUCTURAL: str = "qwen-vl-max"  # structural integrity pass
_QA_MODEL_DASHSCOPE_SEMANTIC: str = "qwen-vl-max"    # semantic quality pass
_QA_MODEL_OPENROUTER_STRUCTURAL: str = "google/gemini-2.5-flash-preview"  # structural (OpenRouter)
_QA_MODEL_OPENROUTER_SEMANTIC: str = "google/gemini-2.5-flash-preview"    # semantic (OpenRouter)


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


def _call_vision_model(content_parts: list[dict], model: str, api_url: str, api_key: str) -> dict:
    """Call a vision model and return parsed JSON response.

    Returns dict with at minimum {quality, qa_reason} keys.
    Raises on network/parse failure.
    """
    import httpx

    resp = httpx.post(
        api_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": 400,
            "temperature": 0.1,
        },
        timeout=90.0,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()

    # Parse JSON — handle markdown fencing
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    return json.loads(text)


def _qwen_visual_qa(prompt: str, frames_b64: list[str], visual_style: str = "") -> dict:
    """Two-pass visual QA ensemble for AI-generated video clips.

    Pass 1 — STRUCTURAL INTEGRITY (fast, strict):
        Detects fundamentally broken output: grid artifacts, corrupted data,
        body horror, overt AI wonk.  Returns REJECTED immediately if found.

    Pass 2 — SEMANTIC QUALITY (only if Pass 1 clears):
        Evaluates prompt adherence, visual style conformance, artistic merit.
        Returns rejected/poor/good/excellent.

    Returns dict with keys: quality ("rejected"/"poor"/"good"/"excellent"),
    qa_reason (str), qa_pass ("structural"/"semantic").

    Quality levels:
        rejected  — fundamentally broken: grid artifacts, digital noise,
                    corrupted data, body horror, overt AI wonk.  NOT usable.
        poor      — passed sanity checks but barely: wrong medium, bad
                    prompt adherence, significant but non-horrific artifacts.
        good      — acceptable quality with minor imperfections.
        excellent — high quality, matches prompt and style closely.

    Args:
        prompt: The generation prompt for this clip.
        frames_b64: Base64-encoded JPEG frames (start, middle, end).
        visual_style: Movie-level visual style description.
    """
    if not _QA_BACKEND:
        raise RuntimeError(
            "OTIO VIOLATION: visual QA unavailable — neither DASHSCOPE_API_KEY "
            "nor OPENROUTER_API_KEY is set. QA is mandatory; the pipeline "
            "cannot accept clips without quality verification."
        )

    # Select backend URLs and models
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

    # Build image content parts (shared between both passes)
    image_parts = []
    for b64 in frames_b64:
        image_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    # ── Pass 1: Structural Integrity ──────────────────────────────────────
    structural_parts = list(image_parts)
    structural_parts.append({
        "type": "text",
        "text": (
            "You are a video output integrity checker. Your ONLY job is to \n"
            "detect fundamentally broken AI-generated video output.\n\n"
            f"These {len(frames_b64)} frames are sampled from an AI-generated video clip.\n\n"
            "Check for ANY of these defects:\n\n"
            "1. CORRUPTED OUTPUT: grid patterns, repeating tile artifacts, \n"
            "   digital noise, static, solid color frames, no discernible content.\n\n"
            "2. OVERT AI WONK: uncanny valley distortion, impossible physics \n"
            "   (objects phasing through each other, gravity violations), \n"
            "   melting/morphing shapes, flickering geometry, surreal \n"
            "   nonsensical compositions that no real camera could capture.\n\n"
            "3. BODY HORROR: deformed human faces or limbs, extra/missing \n"
            "   fingers, fused body parts, distorted eyes/teeth, inhuman \n"
            "   proportions that are clearly unintentional and disturbing.\n\n"
            "If ANY of the above defects are present, the output is REJECTED.\n"
            "If the frames show actual coherent imagery (even if low quality), \n"
            "it PASSES.\n\n"
            "Respond in EXACTLY this JSON format (no markdown):\n"
            '{"verdict": "rejected|pass", "defects": "description of defects found, or none"}'
        ),
    })

    try:
        logger.info("QA Pass 1 (structural) using %s (model=%s)", _QA_BACKEND, structural_model)
        p1 = _call_vision_model(structural_parts, structural_model, api_url, api_key)
        p1_verdict = p1.get("verdict", "pass").lower()
        p1_defects = p1.get("defects", "none")
        logger.info("QA Pass 1 result: verdict=%s, defects=%.120s", p1_verdict, p1_defects)

        if p1_verdict == "rejected":
            return {
                "quality": "rejected",
                "qa_reason": f"STRUCTURAL INTEGRITY FAILURE: {p1_defects}",
                "qa_pass": "structural",
            }
    except Exception as e:
        logger.error("QA Pass 1 (structural) failed: %s", e, exc_info=True)
        # If structural check itself fails, proceed to semantic pass
        # (don't block on QA infrastructure failures)

    # ── Pass 2: Semantic Quality ──────────────────────────────────────────
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

    semantic_parts = list(image_parts)
    semantic_parts.append({
        "type": "text",
        "text": (
            f"You are a video quality assessor for AI-generated footage.\n\n"
            f"The video was generated from this prompt:\n"
            f'"{prompt}"\n'
            f"{style_section}\n"
            f"These {len(frames_b64)} frames are sampled from the start, middle, "
            f"and end of the clip.\n\n"
            f"The frames have already passed structural integrity checks (no \n"
            f"corruption or grid artifacts). Now evaluate QUALITY:\n\n"
            f"REJECTED — Overt AI wonk or body horror that passed the structural \n"
            f"  check: uncanny valley faces, deformed limbs, impossible anatomy, \n"
            f"  melting objects, reality-breaking physics. These are errors, not \n"
            f"  style choices.\n\n"
            f"POOR — Shows actual imagery but barely acceptable:\n"
            f"  - Wrong medium (cartoon when photorealism required)\n"
            f"  - Noticeable but non-horrific AI artifacts\n"
            f"  - Complete prompt mismatch\n\n"
            f"GOOD — Acceptable quality with minor imperfections.\n\n"
            f"EXCELLENT — High quality, matches prompt and style closely.\n\n"
            f"Be LENIENT on minor imperfections (slight blur, small lighting \n"
            f"differences).\n\n"
            f"Respond in EXACTLY this JSON format (no markdown, no extra text):\n"
            f'{{"quality": "rejected|poor|good|excellent", "qa_reason": "brief '
            f'description of what the video shows and why you rated it this way"}}'
        ),
    })

    try:
        logger.info("QA Pass 2 (semantic) using %s (model=%s)", _QA_BACKEND, semantic_model)
        p2 = _call_vision_model(semantic_parts, semantic_model, api_url, api_key)
        quality = p2.get("quality", "unknown").lower()
        qa_reason = p2.get("qa_reason", "No reason provided")

        if quality not in ("rejected", "poor", "good", "excellent"):
            quality = "unknown"

        return {"quality": quality, "qa_reason": qa_reason, "qa_pass": "semantic"}

    except Exception as e:
        # GAP 3.2: Fail-closed — QA infrastructure failure means "poor",
        # not "unknown".  Unknown clips silently pass; poor clips get rejected.
        logger.error("QA Pass 2 (semantic) failed: %s", e, exc_info=True)
        return {"quality": "poor", "qa_reason": f"QA request failed (fail-closed): {e}", "qa_pass": "error"}


# ---------------------------------------------------------------------------
# Video generation (LTX-2.3 via official Lightricks ltx-pipelines)
# ---------------------------------------------------------------------------

@torch.inference_mode()
def _ltx_generate_once(
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    num_frames: int,
    fps: float,
    seed: int,
    num_inference_steps: int,
    guidance_scale: float,
    stg_scale: float,
    modality_scale: float,
    guidance_rescale: float,
    stg_blocks: list[int],
) -> np.ndarray:
    """Run a single ltx-pipelines generation and return numpy uint8 frames.

    The pipeline returns an Iterator[torch.Tensor] of video chunks (uint8,
    shape (T, H, W, C)) and an Audio object.  We collect the video chunks
    into a single numpy array for QA evaluation.

    Memory strategy:
        The 22B transformer weighs ~44GB at bf16.  The pipeline uses a
        block-based lifecycle that loads/unloads components sequentially
        (text encoder → transformer → VAE decoder), so peak VRAM is
        dominated by the transformer alone (~44GB + activations).

        CRITICAL: @torch.inference_mode() is required.  Without it,
        PyTorch retains autograd computation graphs for every intermediate
        tensor across all transformer layers and all diffusion steps.
        This inflates VRAM from ~47GB (weights + activations) to ~140GB
        (weights + autograd graphs), causing OOM on 141GB H200 GPUs.
        The ltx-pipelines library's own main() uses this decorator;
        our worker must too.

        Additional VRAM hygiene:
        1. gc.collect() — release Python references to GPU tensors
        2. torch.cuda.empty_cache() — return cached blocks to the allocator
        3. try/finally — ensure cleanup even if pipeline.__call__ raises
        4. Log VRAM so we can diagnose OOM remotely
        5. expandable_segments=True — prevent allocator fragmentation
    """
    from ltx_core.components.guiders import MultiModalGuiderParams

    # Aggressive pre-generation VRAM cleanup
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()

    # Log VRAM state for remote diagnosis
    if torch.cuda.is_available():
        _alloc = torch.cuda.memory_allocated() / 1e9
        _resv = torch.cuda.memory_reserved() / 1e9
        _total = torch.cuda.get_device_properties(0).total_memory / 1e9
        logger.info(
            "VRAM before pipeline call: allocated=%.2fGB reserved=%.2fGB "
            "total=%.2fGB free=%.2fGB expandable_segments=%s",
            _alloc, _resv, _total, _total - _resv,
            os.environ.get("PYTORCH_CUDA_ALLOC_CONF", "NOT SET"),
        )

    video_guider = MultiModalGuiderParams(
        cfg_scale=guidance_scale,
        stg_scale=stg_scale,
        rescale_scale=guidance_rescale,
        modality_scale=modality_scale,
        skip_step=0,
        stg_blocks=stg_blocks,
    )
    audio_guider = MultiModalGuiderParams(
        cfg_scale=7.0,
        stg_scale=stg_scale,
        rescale_scale=guidance_rescale,
        modality_scale=modality_scale,
        skip_step=0,
        stg_blocks=stg_blocks,
    )

    try:
        video_iter, _audio = _ltx_pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            height=height,
            width=width,
            num_frames=num_frames,
            frame_rate=fps,
            num_inference_steps=num_inference_steps,
            video_guider_params=video_guider,
            audio_guider_params=audio_guider,
            images=[],  # text-to-video, no image conditioning
        )

        # Collect video chunks from iterator into numpy array
        chunks = []
        for chunk in video_iter:
            # chunk is torch.Tensor (T, H, W, C) uint8 on CPU
            chunks.append(chunk.cpu().numpy())
        candidate_frames = np.concatenate(chunks, axis=0)  # (T, H, W, C) uint8
    except Exception:
        # On OOM or any pipeline error, force-free everything before
        # re-raising so that the next retry starts with a clean slate
        # instead of accumulating leaked transformers.
        logger.warning(
            "Pipeline call failed — running emergency VRAM cleanup"
        )
        gc.collect()
        torch.cuda.empty_cache()
        raise
    finally:
        # Always reclaim VRAM after generation, whether it succeeded
        # or failed.  This pairs with the pre-generation cleanup above
        # to bound peak memory across retry attempts.
        gc.collect()
        torch.cuda.empty_cache()
        if torch.cuda.is_available():
            _alloc = torch.cuda.memory_allocated() / 1e9
            _resv = torch.cuda.memory_reserved() / 1e9
            logger.info(
                "VRAM after pipeline call: allocated=%.2fGB "
                "reserved=%.2fGB",
                _alloc, _resv,
            )

    return candidate_frames


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
    stg_scale: float = 1.0,
    modality_scale: float = 3.0,
    guidance_rescale: float = 0.7,
    stg_blocks: list[int] | None = None,
) -> tuple[bytes, dict]:
    """Generate video clip using LTX-2.3 dev (full) model via ltx-pipelines.

    Returns (raw_mp4_bytes, qa_status) where qa_status follows bearnaise
    pattern: {quality, qa_reason, attempts, seed}.
    Caller must hold _model_lock.

    Args:
        negative_prompt: Per-clip negative prompt from visual_style.avoid.
            Merged with baseline negatives.
        visual_style: Movie-level visual style description passed to QA.
        stg_scale: Spatio-temporal guidance scale (LTX-2.3 specific).
        modality_scale: Modality guidance scale (video vs audio balance).
        guidance_rescale: CFG rescale factor to reduce oversaturation.
        stg_blocks: Which transformer blocks to apply STG to.
    """
    # Fail fast if no QA backend — don't waste GPU time generating frames
    # that will be rejected anyway.  _QA_BACKEND is a module-level constant.
    if not _QA_BACKEND:
        raise RuntimeError(
            "OTIO VIOLATION: visual QA unavailable — neither DASHSCOPE_API_KEY "
            "nor OPENROUTER_API_KEY is set. Refusing to generate video without QA."
        )

    _load_ltx()

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

    # LTX-2.3 spatio-temporal guidance block indices
    _stg_blocks = stg_blocks if stg_blocks is not None else [28]

    # Retry loop with Qwen-Omni visual QA (bearnaise pattern).
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

        # Reclaim fragmented VRAM before each attempt — with layer
        # streaming headroom is ample, but cleanup prevents accumulation.
        gc.collect()
        torch.cuda.empty_cache()

        candidate_frames = _ltx_generate_once(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            num_frames=num_frames,
            fps=fps,
            seed=current_seed,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            stg_scale=stg_scale,
            modality_scale=modality_scale,
            guidance_rescale=guidance_rescale,
            stg_blocks=_stg_blocks,
        )

        logger.info(
            "Generation complete (attempt %d/%d): shape=%s, dtype=%s",
            attempt, max_attempts, candidate_frames.shape, candidate_frames.dtype,
        )

        # Sanitise NaN/Inf (bfloat16 numerical instability during VAE decode)
        if np.isnan(candidate_frames).any() or np.isinf(candidate_frames).any():
            nan_frac = float(np.isnan(candidate_frames).mean())
            logger.warning(
                "NaN/Inf in decoded frames (%.1f%% NaN, attempt %d/%d, seed=%d)",
                nan_frac * 100, attempt, max_attempts, current_seed,
            )
            candidate_frames = np.nan_to_num(
                candidate_frames, nan=0, posinf=255, neginf=0
            ).astype(np.uint8)

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

        # Stage 2: Qwen-Omni visual QA (semantic evaluation)
        n = candidate_frames.shape[0]
        sample_indices = [0, n // 2, n - 1] if n >= 3 else list(range(n))
        frames_b64 = _frames_to_base64(candidate_frames, sample_indices)
        qa_result = _qwen_visual_qa(prompt, frames_b64, visual_style=visual_style)

        logger.info(
            "Qwen QA (attempt %d/%d): quality=%s, reason=%.120s",
            attempt, max_attempts, qa_result["quality"],
            qa_result.get("qa_reason", ""),
        )

        if qa_result["quality"] == "rejected":
            logger.error(
                "QA REJECTED output (attempt %d/%d): %s",
                attempt, max_attempts, qa_result.get("qa_reason", ""),
            )
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

    # GAP 3.2: Fail-closed — if best QA is still "unknown" after all
    # attempts, treat as "poor" so the client rejects it.
    if best_passing_qa.get("quality") == "unknown":
        logger.error(
            "QA still 'unknown' after %d attempts — fail-closed to 'poor'",
            max_attempts,
        )
        best_passing_qa["quality"] = "poor"
        best_passing_qa["qa_reason"] = (
            f"Fail-closed: QA could not evaluate after {max_attempts} attempts. "
            + best_passing_qa.get("qa_reason", "")
        )

    # Prefer any brightness-passing frame over a brightness-failing one
    if best_passing_frames is not None:
        video_frames = best_passing_frames
        best_seed = best_passing_seed
        best_qa = best_passing_qa
    else:
        video_frames = best_failing_frames
        best_seed = best_failing_seed
        best_qa = {"quality": "poor", "qa_reason": "All attempts failed brightness check (fail-closed)"}

    elapsed = time.time() - t0
    logger.info("Video generated in %.1fs (seed=%d, qa=%s)",
                elapsed, best_seed, best_qa.get("quality", "unknown"))

    # Export to MP4 using ffmpeg (video_frames is uint8 numpy)
    raw_path = os.path.join(
        _output_dir, f"clip_{uuid.uuid4().hex[:8]}_raw.mp4"
    )
    output_path = os.path.join(
        os.path.dirname(raw_path),
        os.path.basename(raw_path).replace("_raw.mp4", ".mp4"),
    )
    os.makedirs(_output_dir, exist_ok=True)

    # Write raw frames via ffmpeg pipe (no diffusers dependency)
    _ffmpeg_write = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-s", f"{video_frames.shape[2]}x{video_frames.shape[1]}",
            "-pix_fmt", "rgb24",
            "-r", str(fps),
            "-i", "-",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            output_path,
        ],
        input=video_frames.tobytes(),
        capture_output=True, timeout=120,
    )
    if _ffmpeg_write.returncode != 0:
        stderr_msg = (_ffmpeg_write.stderr or b"")[:500]
        logger.error(
            "ffmpeg encode failed (rc=%d): %s",
            _ffmpeg_write.returncode,
            stderr_msg,
        )
        # Architecture invariant: never silently degrade.  Raise so the
        # recovery middleware can retry or escalate instead of passing
        # invalid bytes through the pipeline.
        raise RuntimeError(
            f"ffmpeg video encoding failed (rc={_ffmpeg_write.returncode}): "
            f"{stderr_msg}"
        )

    with open(output_path, "rb") as f:
        data = f.read()

    # Clean up temp files
    for p in {raw_path, output_path}:
        try:
            os.unlink(p)
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
async def health():
    """Quick health check — backward-compatible with provisioner.

    If the VMAgent is running, delegate to it (richer data).
    Otherwise fall back to the minimal HealthResponse.
    """
    if _vm_agent is not None:
        return _vm_agent.get_health_response()

    # Fallback for legacy callers (should not happen in normal operation)
    gpu_name = "unknown"
    vram_used = 0.0
    vram_total = 0.0
    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)
        vram_used = torch.cuda.memory_allocated(0) / 1e9
        vram_total = torch.cuda.get_device_properties(0).total_memory / 1e9

    status = "error" if _bootstrap_status.phase == "error" else "ok"
    return HealthResponse(
        status=status,
        gpu=gpu_name,
        tts_loaded=_tts_model is not None,
        ltx_loaded=_ltx_pipe is not None,
        vram_used_gb=round(vram_used, 2),
        vram_total_gb=round(vram_total, 2),
        bootstrap=_bootstrap_status,
        # GAP 5.1: Model identity
        tts_model_name="Qwen3-TTS" if _tts_model is not None else "",
        ltx_model_path=str(_models_dir) + "/ltx2" if _ltx_pipe is not None else "",
        worker_mode=_worker_mode,
    )


@app.get("/status")
async def status_endpoint():
    """Full VM agent status — the overseer reads this.

    Includes bootstrap, models, self-monitoring health, escalations,
    and task tracking.  This is the single source of truth for what
    this VM is doing.
    """
    if _vm_agent is None:
        raise HTTPException(503, "VMAgent not initialised yet")
    return _vm_agent.get_status()


@app.get("/escalations")
async def escalations_endpoint():
    """Return unacknowledged escalation events for the overseer."""
    if _vm_agent is None:
        raise HTTPException(503, "VMAgent not initialised yet")
    with _vm_agent._lock:
        unacked = [
            e.to_dict() for e in _vm_agent._escalations if not e.acked
        ]
    return {"escalations": unacked, "count": len(unacked)}


@app.post("/escalations/ack")
async def ack_escalations_endpoint(before_ts: float):
    """Acknowledge escalations up to a given timestamp.

    Called by the central overseer after it has processed the events.
    """
    if _vm_agent is None:
        raise HTTPException(503, "VMAgent not initialised yet")
    count = _vm_agent.ack_escalations(before_ts)
    return {"acknowledged": count}


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
    success = False

    with _model_lock:
        try:
            audio_array, sr = _generate_tts(req.text, req.voice, req.language)
            success = True
        except Exception as e:
            logger.error("TTS failed: %s", e, exc_info=True)
            if _vm_agent:
                _vm_agent.record_task(False, time.time() - t0)
                _vm_agent.escalate(
                    Severity.WARNING, "generation",
                    f"TTS generation failed: {e}",
                )
            raise HTTPException(500, f"TTS generation failed: {e}")

    # Encode as WAV (no lock needed — local data only)
    buf = io.BytesIO()
    sf.write(buf, audio_array, sr, format="WAV", subtype="PCM_16")
    wav_bytes = buf.getvalue()

    elapsed = time.time() - t0
    duration = len(audio_array) / sr
    if _vm_agent:
        _vm_agent.record_task(success, elapsed)
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
                stg_scale=req.stg_scale,
                modality_scale=req.modality_scale,
                guidance_rescale=req.guidance_rescale,
                stg_blocks=req.stg_blocks,
            )
        except HTTPException:
            if _vm_agent:
                _vm_agent.record_task(False, time.time() - t0)
            raise
        except Exception as e:
            logger.error("Video failed: %s", e, exc_info=True)
            if _vm_agent:
                _vm_agent.record_task(False, time.time() - t0)
                _vm_agent.escalate(
                    Severity.WARNING, "generation",
                    f"Video generation failed: {e}",
                )
            raise HTTPException(500, f"Video generation failed: {e}")

    elapsed = time.time() - t0
    if _vm_agent:
        _vm_agent.record_task(True, elapsed)
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
                qa_status.get("qa_reason", "").encode("utf-8")
            ).decode("ascii"),
            "X-QA-Attempts": str(qa_status.get("attempts", 1)),
            "X-QA-Seed": str(qa_status.get("seed", req.seed)),
        },
    )


@app.post("/assign-clip")
def assign_clip_endpoint(req: dict):
    """Fleet push endpoint — coordinator assigns a priority clip to this worker.

    Used for priority retries when the pull model is too slow.
    The coordinator POSTs a clip spec directly to this worker, which
    generates it immediately and reports the result back.

    Expected request body::
        {
            "clip_id": "scene_003_phrase_001",
            "prompt": "...",
            "negative_prompt": "...",
            "duration": 7.5,
            "lora_id": "documentary-realism",
            "lora_weight": 0.7,
            "width": 512,
            "height": 320,
            "num_frames": 105,
            "callback_url": "http://backend:8000/fleet/report"
        }
    """
    if _worker_mode == "tts":
        raise HTTPException(503, "This worker is in TTS-only mode")

    prompt = req.get("prompt", "")
    if not prompt.strip():
        raise HTTPException(400, "Prompt must not be empty")

    clip_id = req.get("clip_id", "unknown")
    callback_url = req.get("callback_url", "")

    logger.info("Assigned clip %s (priority push)", clip_id)
    t0 = time.time()

    with _model_lock:
        try:
            mp4_bytes, qa_status = _generate_video(
                prompt=prompt,
                duration_sec=req.get("duration", 5.0),
                width=req.get("width", 512),
                height=req.get("height", 320),
                num_frames=req.get("num_frames", 105),
                seed=req.get("seed", -1),
                num_inference_steps=req.get("num_inference_steps", 40),
                guidance_scale=req.get("guidance_scale", 3.0),
                negative_prompt=req.get("negative_prompt", ""),
                visual_style=req.get("visual_style", ""),
            )
        except Exception as e:
            elapsed = time.time() - t0
            logger.error("Assigned clip %s failed: %s", clip_id, e)
            if _vm_agent:
                _vm_agent.record_task(False, elapsed)
            # Report failure back to coordinator if callback_url provided
            if callback_url:
                try:
                    import requests as _req
                    _req.post(callback_url, json={
                        "clip_id": clip_id,
                        "worker_id": f"{os.environ.get('VAST_CONTAINERLABEL', 'unknown')}",
                        "success": False,
                        "error": str(e),
                        "error_category": "video_generation",
                    }, timeout=10)
                except Exception:
                    pass
            raise HTTPException(500, f"Clip generation failed: {e}")

    elapsed = time.time() - t0
    if _vm_agent:
        _vm_agent.record_task(True, elapsed)

    # Save the output
    output_path = os.path.join(_output_dir, f"{clip_id}.mp4")
    with open(output_path, "wb") as f:
        f.write(mp4_bytes)

    qa_quality = qa_status.get("quality", "unknown")
    qa_reason = qa_status.get("qa_reason", "")

    # Report success back to coordinator
    if callback_url:
        try:
            import requests as _req
            _req.post(callback_url, json={
                "clip_id": clip_id,
                "worker_id": f"{os.environ.get('VAST_CONTAINERLABEL', 'unknown')}",
                "success": True,
                "output_path": output_path,
                "gen_time": round(elapsed, 2),
                "qa_quality": qa_quality,
                "qa_reason": qa_reason,
            }, timeout=10)
        except Exception as cb_err:
            logger.warning("Failed to report clip %s to coordinator: %s", clip_id, cb_err)

    logger.info(
        "Assigned clip %s done: %.1fs, qa=%s", clip_id, elapsed, qa_quality,
    )

    return Response(
        content=mp4_bytes,
        media_type="video/mp4",
        headers={
            "X-Clip-Id": clip_id,
            "X-Gen-Time": str(round(elapsed, 3)),
            "X-QA-Quality": qa_quality,
        },
    )


@app.get("/verify")
async def verify_endpoint():
    """GAP 7.1: Return model identity info for bootstrap verification.

    Reports which models are loaded, their paths, and basic file sizes
    so the provisioner can verify the correct models were downloaded.
    """
    import glob as globmod

    model_files: dict[str, dict] = {}

    # TTS model
    tts_dir = os.path.join(_models_dir, "qwen3-tts-voicedesign")
    tts_weights = os.path.join(tts_dir, "model.safetensors")
    if os.path.exists(tts_weights):
        model_files["tts"] = {
            "path": tts_weights,
            "size_mb": round(os.path.getsize(tts_weights) / 1048576, 1),
            "loaded": _tts_model is not None,
        }

    # LTX model
    ltx_dir = os.path.join(_models_dir, "ltx2")
    ltx_ckpt = os.path.join(ltx_dir, "ltx-2.3-22b-dev.safetensors")
    if os.path.exists(ltx_ckpt):
        model_files["ltx"] = {
            "path": ltx_ckpt,
            "size_mb": round(os.path.getsize(ltx_ckpt) / 1048576, 1),
            "loaded": _ltx_pipe is not None,
        }

    # Gemma text encoder
    gemma_config = os.path.join(ltx_dir, "gemma", "config.json")
    if os.path.exists(gemma_config):
        gemma_files = globmod.glob(os.path.join(ltx_dir, "gemma", "*"))
        model_files["gemma_text_encoder"] = {
            "path": os.path.join(ltx_dir, "gemma"),
            "file_count": len(gemma_files),
        }

    return {
        "worker_mode": _worker_mode,
        "models_dir": _models_dir,
        "models": model_files,
        "bootstrap_phase": _bootstrap_status.phase,
    }


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
    parser = argparse.ArgumentParser(description="GPU Worker — autonomous VM agent")
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

    global _models_dir, _output_dir, _worker_mode, _vm_agent, _bootstrap_status
    _models_dir = args.models_dir
    _output_dir = args.output_dir
    _worker_mode = args.mode

    os.makedirs(_output_dir, exist_ok=True)

    # --- Create the VMAgent ---
    # The agent manages the full lifecycle: bootstrap, model loading,
    # self-monitoring, recovery, escalation.  FastAPI starts immediately
    # so /health is reachable; the agent runs bootstrap + model loading
    # in a background thread and reports structured status.
    agent = VMAgent(
        worker_mode=_worker_mode,
        models_dir=_models_dir,
        output_dir=_output_dir,
    )
    _vm_agent = agent
    # Share the bootstrap status so legacy /health callers see it too
    _bootstrap_status = agent.bootstrap

    logger.info(
        "Starting VM Agent on %s:%d (mode=%s)",
        args.host, args.port, _worker_mode,
    )
    logger.info("Models dir: %s", _models_dir)
    logger.info("Output dir: %s", _output_dir)

    # Background thread: bootstrap → model loading → start monitoring.
    # FastAPI starts immediately so the /health endpoint is reachable
    # during the bootstrap phase.  The overseer reads structured status
    # from /health and /status to know exactly what's happening.
    def _agent_lifecycle():
        ok = agent.run_bootstrap()
        if ok:
            # Models loaded — start continuous self-monitoring
            agent.start_monitoring()
        else:
            # Bootstrap failed — the agent has already escalated.
            # The self-monitoring loop still starts so we report
            # health even in the error state (e.g. disk, temp).
            agent.start_monitoring()
            logger.error(
                "VMAgent: bootstrap failed — agent is in error state. "
                "Overseer should read /status for details."
            )

    lifecycle_thread = threading.Thread(
        target=_agent_lifecycle, name="vm-agent-lifecycle", daemon=True,
    )
    lifecycle_thread.start()
    logger.info("VMAgent lifecycle thread started")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
