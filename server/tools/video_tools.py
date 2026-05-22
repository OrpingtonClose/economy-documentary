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

    The prompt text is sent raw to the worker.  Duration/resolution are
    local defaults; the caller accepts what the worker returns.
    """
    gpu_worker_url = _get_next_worker_url("video")
    if not gpu_worker_url:
        raise WorkerUnavailableError(
            "No video worker registered. Provision a worker first.",
            stage="video",
        )

    worker_url = f"{gpu_worker_url.rstrip('/')}/"
    req = Request(worker_url, data=prompt.encode("utf-8"), headers={"Content-Type": "text/plain"})

    try:
        with urlopen(req, timeout=600) as resp:
            result = _parse_video_response(resp)
    except URLError as e:
        raise WorkerUnavailableError(
            f"Video worker unreachable at {worker_url}: {e}",
            stage="video",
        ) from e

    with open(output_path, "wb") as f:
        f.write(result["mp4_bytes"])

    return json.dumps({
        "status": "generated",
        "output_path": output_path,
        "actual_duration": duration_sec,
        "qa_quality": result["qa_quality"],
        "qa_reason": result["qa_reason"],
        "qa_attempts": result["qa_attempts"],
        "qa_seed": result["qa_seed"],
        "gen_time": result["gen_time"],
        "size_bytes": len(result["mp4_bytes"]),
    })
