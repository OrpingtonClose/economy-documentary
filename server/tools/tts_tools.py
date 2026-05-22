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
    worker_url: str = "",
    tool_context=None,
) -> str:
    """Generate narration WAV for a scene.

    The narration text is sent raw to the worker.  Voice and language
    are local defaults; the caller accepts what the worker returns.
    """
    if not worker_url:
        raise WorkerUnavailableError(
            "No worker_url provided. Agent must pass one.",
            stage="audio",
        )

    worker_url = f"{worker_url.rstrip('/')}/"
    req = Request(worker_url, data=text.encode("utf-8"), headers={"Content-Type": "text/plain"})

    try:
        with urlopen(req) as resp:
            result = _parse_tts_response(resp)
    except URLError as e:
        raise WorkerUnavailableError(
            f"TTS worker unreachable at {worker_url}: {e}",
            stage="audio",
        ) from e

    if not output_dir:
        raise WorkerUnavailableError(
            "No output_dir provided. Pass it explicitly.",
            stage="audio",
        )
    os.makedirs(os.path.join(output_dir, "audio"), exist_ok=True)
    wav_path = os.path.join(output_dir, "audio", f"scene_{scene_num:03d}_narration.wav")

    with open(wav_path, "wb") as f:
        f.write(result["wav_bytes"])

    return json.dumps({
        "status": "generated",
        "scene_num": scene_num,
        "wav_path": wav_path,
        "duration_sec": result["duration"],
        "sample_rate": result["sample_rate"],
        "gen_time": result["gen_time"],
        "size_bytes": len(result["wav_bytes"]),
    })
