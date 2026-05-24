"""Video tools — call LTX-2.3 GPU worker to generate MP4 clips.

The interface to the worker is pure text over HTTP:
  GET /  → plain text status
  POST / → plain text prompt, returns MP4 bytes

No JSON, no schemas, no structured anything.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from urllib.request import Request, urlopen
from urllib.error import URLError

from pipeline_errors import (
    WorkerUnavailableError,
    ArtifactValidationError,
)
from worker_provisioner import _get_next_worker_url

logger = logging.getLogger(__name__)


def _parse_video_response(resp) -> dict:
    """Read MP4 bytes and metadata headers from worker response."""
    result_bytes = resp.read()
    if not result_bytes:
        raise ArtifactValidationError("GPU worker returned empty response", stage="video")
    if b"ftyp" not in result_bytes[:64]:
        raise ArtifactValidationError(
            f"GPU worker returned non-MP4 data (first 8 bytes: {result_bytes[:8]!r})",
            stage="video",
        )
    return {
        "mp4_bytes": result_bytes,
        "qa_quality": resp.headers.get("X-QA-Quality", "unknown"),
        "qa_reason": resp.headers.get("X-QA-Reason", ""),
        "qa_attempts": int(resp.headers.get("X-QA-Attempts", "1")),
        "qa_seed": int(resp.headers.get("X-QA-Seed", "42")),
        "gen_time": float(resp.headers.get("X-Gen-Time", "0")),
    }


def generate_video_clip(
    prompt: str,
    duration_sec: float,
    lora_id: str,
    lora_weight: float,
    output_path: str,
    negative_prompt: str = "",
    visual_style: str = "",
    tool_context=None,
) -> str:
    """Generate a video clip using LTX-2.3.

    DEPRECATED: This direct-worker path is dead. Use the job queue
    (submit_render_job) and let the provisioner agent dispatch.
    """
    raise RuntimeError(
        "generate_video_clip direct-worker path is dead. "
        "Use submit_render_job(stage='video', job_type='video_render') instead."
    )
