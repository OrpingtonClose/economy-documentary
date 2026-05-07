"""
GPU protocol — extracted interface for GPU job submission and monitoring.

Decouples GPU job management from the ADK FunctionTool wrappers in
``server/tools/vastai_tools.py`` and ``server/tools/video_tools.py``.
In the Strands architecture, GPU jobs are submitted via Temporal
workflows for durability (heartbeat, retry, VM preemption handling).

This module defines the protocol interface. The actual implementations
can be swapped:
  - Vast.ai (current production)
  - Local GPU (development)
  - Mock (testing via Substrate)

Architecture::

    submit_gpu_job(params)  →  Temporal workflow  →  GPU worker
         ↑                                              ↓
    check_gpu_job(job_id)  ←  heartbeat/retry    ←  result
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GPU job types
# ---------------------------------------------------------------------------


class GPUJobType(str, Enum):
    """Types of GPU workloads the pipeline dispatches."""
    VIDEO_RENDER = "video_render"
    TTS_RENDER = "tts_render"
    WHISPERX_ALIGN = "whisperx_align"
    LORA_TRAIN = "lora_train"
    SCENE_ASSEMBLY = "scene_assembly"


class GPUJobStatus(str, Enum):
    """Lifecycle states of a GPU job."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PREEMPTED = "preempted"  # VM was preempted; Temporal will retry


# ---------------------------------------------------------------------------
# Job data
# ---------------------------------------------------------------------------


@dataclass
class GPUJobRequest:
    """A request to submit a GPU job."""
    job_type: GPUJobType
    params: dict[str, Any] = field(default_factory=dict)
    scene_num: int = 0
    phrase_idx: int = 0
    priority: int = 0
    timeout_seconds: float = 600.0
    max_retries: int = 3


@dataclass
class GPUJobResult:
    """The result of a GPU job."""
    job_id: str
    job_type: GPUJobType
    status: GPUJobStatus
    output_path: str = ""
    duration_seconds: float = 0.0
    error: str = ""
    cost_usd: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0


# ---------------------------------------------------------------------------
# Protocol interface
# ---------------------------------------------------------------------------


class GPUProtocol:
    """Abstract interface for GPU job management.

    Implementations:
      - VastAIProtocol (production — wraps Vast.ai API + Temporal)
      - LocalGPUProtocol (dev — runs on local GPU)
      - MockGPUProtocol (testing — instant completion/failure)
    """

    async def submit(self, request: GPUJobRequest) -> GPUJobResult:
        """Submit a GPU job for execution."""
        raise NotImplementedError

    async def check(self, job_id: str) -> GPUJobResult:
        """Check the status of a GPU job."""
        raise NotImplementedError

    async def cancel(self, job_id: str) -> GPUJobResult:
        """Cancel a running GPU job."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
