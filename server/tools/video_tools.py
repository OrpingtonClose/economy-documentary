"""
Video generation tools -- LTX-2.3 + ffprobe wrappers.

For production: generates video clips using LTX-2.3 on GPU VM.
For test run: generates solid-color MP4 files with correct duration using ffmpeg.

Rules:
- Duration should be target_duration * 1.15 (15% longer for trim margin)
- bf16 only, no FP8, no quantization
- All subprocess calls use list form (no shell=True)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Optional

from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

_OUTPUT_BASE = os.environ.get(
    "VIDEO_OUTPUT_DIR", "/tmp/documentary-pipeline/video"
)
_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "").strip().lower() in ("1", "true")
_TRIM_MARGIN = 1.15  # 15% longer for trim margin


def _generate_solid_color_mp4(
    output_path: str,
    duration: float,
    width: int = 1280,
    height: int = 720,
    fps: int = 24,
    color: str = "0x336699",
) -> bool:
    """Generate a solid-color MP4 file using ffmpeg (for testing)."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"color=c={color}:s={width}x{height}:d={duration:.2f}:r={fps}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-t", f"{duration:.2f}",
        output_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.error("ffmpeg failed: %s", e)
        return False


def generate_video_clip(
    prompt: str,
    duration_sec: float,
    lora_id: str,
    lora_weight: float,
    output_path: str,
    tool_context=None,
) -> str:
    """Generate a video clip using LTX-2.3.

    Args:
        prompt: Visual description prompt for video generation.
        duration_sec: Target duration in seconds (will be extended by 15%).
        lora_id: LoRA style identifier.
        lora_weight: LoRA weight (0.0-1.0).
        output_path: Path for the output MP4 file.

    Returns:
        JSON string with generation results.
    """
    actual_duration = duration_sec * _TRIM_MARGIN

    if _TEST_MODE:
        success = _generate_solid_color_mp4(output_path, actual_duration)
        if not success:
            return json.dumps(
                {
                    "status": "error",
                    "error": "Failed to generate test video via ffmpeg",
                }
            )

        logger.info(
            "Test mode: generated solid-color MP4 %s (%.2fs)",
            output_path,
            actual_duration,
        )
        return json.dumps(
            {
                "status": "generated",
                "mode": "test",
                "output_path": output_path,
                "target_duration": round(duration_sec, 2),
                "actual_duration": round(actual_duration, 2),
                "lora_id": lora_id,
                "lora_weight": lora_weight,
                "resolution": "1280x720",
                "fps": 24,
            }
        )

    # Production mode: call LTX-2.3 on GPU VM
    # TODO: Implement actual LTX-2.3 generation call
    # For now, generate solid-color placeholder
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    success = _generate_solid_color_mp4(output_path, actual_duration)

    if not success:
        return json.dumps(
            {
                "status": "error",
                "error": "Failed to generate placeholder video",
            }
        )

    logger.info(
        "Generated video clip %s (%.2fs, lora=%s@%.2f)",
        output_path,
        actual_duration,
        lora_id,
        lora_weight,
    )
    return json.dumps(
        {
            "status": "generated",
            "mode": "placeholder",
            "output_path": output_path,
            "target_duration": round(duration_sec, 2),
            "actual_duration": round(actual_duration, 2),
            "lora_id": lora_id,
            "lora_weight": lora_weight,
            "prompt_preview": prompt[:200],
        }
    )


def probe_clip(mp4_path: str, tool_context=None) -> str:
    """Probe an MP4 file for duration, resolution, and FPS using ffprobe.

    Args:
        mp4_path: Path to the MP4 file.

    Returns:
        JSON string with clip metadata.
    """
    if not os.path.exists(mp4_path):
        return json.dumps({"error": f"File not found: {mp4_path}"})

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        mp4_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return json.dumps(
                {
                    "error": f"ffprobe failed (rc={result.returncode})",
                    "stderr": result.stderr[:500],
                }
            )

        probe_data = json.loads(result.stdout)

        # Extract info from first video stream
        video_stream = None
        for stream in probe_data.get("streams", []):
            if stream.get("codec_type") == "video":
                video_stream = stream
                break

        duration = float(probe_data.get("format", {}).get("duration", 0))
        width = int(video_stream.get("width", 0)) if video_stream else 0
        height = int(video_stream.get("height", 0)) if video_stream else 0
        fps_str = video_stream.get("r_frame_rate", "0/1") if video_stream else "0/1"

        # Parse fractional FPS
        fps_parts = fps_str.split("/")
        if len(fps_parts) == 2 and int(fps_parts[1]) > 0:
            fps = round(int(fps_parts[0]) / int(fps_parts[1]), 2)
        else:
            fps = float(fps_parts[0]) if fps_parts[0] else 0.0

        return json.dumps(
            {
                "mp4_path": mp4_path,
                "duration": round(duration, 3),
                "width": width,
                "height": height,
                "fps": fps,
                "resolution": f"{width}x{height}",
            }
        )

    except subprocess.TimeoutExpired:
        return json.dumps({"error": "ffprobe timed out"})
    except (json.JSONDecodeError, ValueError) as e:
        return json.dumps({"error": f"ffprobe output parse error: {e}"})


# -- ADK FunctionTool wrappers -------------------------------------------------
generate_video_clip_tool = FunctionTool(generate_video_clip)
probe_clip_tool = FunctionTool(probe_clip)

video_tools = [generate_video_clip_tool, probe_clip_tool]
