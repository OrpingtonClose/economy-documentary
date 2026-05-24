"""TTS tools — call Qwen3-TTS GPU worker to generate narration WAV.

The interface to the worker is pure text over HTTP:
  GET /  → plain text status
  POST / → plain text narration, returns WAV bytes

No JSON, no schemas, no structured anything.
"""
from __future__ import annotations

import json
import logging
import os
import wave
from urllib.request import Request, urlopen
from urllib.error import URLError

from pipeline_errors import (
    WorkerUnavailableError,
    ArtifactValidationError,
)
from worker_provisioner import _get_next_worker_url

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 24000


def _parse_tts_response(resp) -> dict:
    """Read WAV bytes and metadata headers from worker response."""
    result_bytes = resp.read()
    if not result_bytes:
        raise ArtifactValidationError("TTS worker returned empty response", stage="audio")
    if not result_bytes.startswith(b"RIFF"):
        raise ArtifactValidationError(
            f"TTS worker returned non-WAV data (first 4 bytes: {result_bytes[:4]!r})",
            stage="audio",
        )
    return {
        "wav_bytes": result_bytes,
        "duration": float(resp.headers.get("X-Audio-Duration", "0")),
        "sample_rate": int(resp.headers.get("X-Sample-Rate", str(_SAMPLE_RATE))),
        "gen_time": float(resp.headers.get("X-Gen-Time", "0")),
    }


def generate_narration(
    scene_num: int,
    voice_role: str,
    text: str,
    output_dir: str = "",
    language: str = "",
    tool_context=None,
) -> str:
    """Generate narration WAV for a scene.

    DEPRECATED: This direct-worker path is dead. Use the job queue
    (submit_render_job) and let the provisioner agent dispatch.
    """
    raise RuntimeError(
        "generate_narration direct-worker path is dead. "
        "Use submit_render_job(stage='audio', job_type='narration') instead."
    )
        "wav_path": wav_path,
        "duration_sec": result["duration"],
        "sample_rate": result["sample_rate"],
        "gen_time": result["gen_time"],
        "size_bytes": len(result["wav_bytes"]),
    })
