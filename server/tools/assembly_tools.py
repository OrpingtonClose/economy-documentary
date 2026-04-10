"""
Assembly tools -- ffmpeg wrappers for muxing, concat, and trim.

Rules:
- mux_audio_video: NO -shortest flag (video must be >= audio)
- All subprocess calls use list form (no shell=True)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from typing import List

from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)


def mux_audio_video(
    audio_path: str,
    video_path: str,
    output_path: str,
    tool_context=None,
) -> str:
    """Mux audio and video into a single file.

    No -shortest flag: video must be >= audio duration.

    Args:
        audio_path: Path to the audio file (WAV/AAC).
        video_path: Path to the video file (MP4).
        output_path: Path for the muxed output file.

    Returns:
        JSON string with mux result.
    """
    for path, label in [(audio_path, "audio"), (video_path, "video")]:
        if not os.path.exists(path):
            return json.dumps({"error": f"{label} file not found: {path}"})

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-map", "0:v:0",
        "-map", "1:a:0",
        output_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return json.dumps(
                {
                    "error": f"ffmpeg mux failed (rc={result.returncode})",
                    "stderr": result.stderr[:500],
                }
            )

        logger.info("Muxed %s + %s -> %s", video_path, audio_path, output_path)
        return json.dumps(
            {
                "status": "muxed",
                "output_path": output_path,
                "audio_path": audio_path,
                "video_path": video_path,
            }
        )

    except subprocess.TimeoutExpired:
        return json.dumps({"error": "ffmpeg mux timed out"})


def concat_clips(
    clip_paths: str,
    output_path: str,
    tool_context=None,
) -> str:
    """Concatenate a list of video clips using ffmpeg concat demuxer.

    Args:
        clip_paths: Comma-separated list of clip file paths.
        output_path: Path for the concatenated output file.

    Returns:
        JSON string with concat result.
    """
    paths = [p.strip() for p in clip_paths.split(",") if p.strip()]

    if not paths:
        return json.dumps({"error": "No clip paths provided"})

    for path in paths:
        if not os.path.exists(path):
            return json.dumps({"error": f"Clip not found: {path}"})

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # Create concat list file
    concat_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False
    )
    try:
        for path in paths:
            concat_file.write(f"file '{path}'\n")
        concat_file.close()

        cmd = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_file.name,
            "-c", "copy",
            output_path,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return json.dumps(
                {
                    "error": f"ffmpeg concat failed (rc={result.returncode})",
                    "stderr": result.stderr[:500],
                }
            )

        logger.info("Concatenated %d clips -> %s", len(paths), output_path)
        return json.dumps(
            {
                "status": "concatenated",
                "output_path": output_path,
                "num_clips": len(paths),
                "clip_paths": paths,
            }
        )

    except subprocess.TimeoutExpired:
        return json.dumps({"error": "ffmpeg concat timed out"})
    finally:
        os.unlink(concat_file.name)


def trim_clip(
    input_path: str,
    start_sec: float,
    duration_sec: float,
    output_path: str,
    tool_context=None,
) -> str:
    """Trim a clip using OTIO source_range parameters.

    Args:
        input_path: Path to the input video file.
        start_sec: Start time in seconds.
        duration_sec: Duration to extract in seconds.
        output_path: Path for the trimmed output file.

    Returns:
        JSON string with trim result.
    """
    if not os.path.exists(input_path):
        return json.dumps({"error": f"Input file not found: {input_path}"})

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", f"{start_sec:.3f}",
        "-i", input_path,
        "-t", f"{duration_sec:.3f}",
        "-c", "copy",
        output_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return json.dumps(
                {
                    "error": f"ffmpeg trim failed (rc={result.returncode})",
                    "stderr": result.stderr[:500],
                }
            )

        logger.info(
            "Trimmed %s -> %s (start=%.2f, dur=%.2f)",
            input_path,
            output_path,
            start_sec,
            duration_sec,
        )
        return json.dumps(
            {
                "status": "trimmed",
                "output_path": output_path,
                "input_path": input_path,
                "start_sec": start_sec,
                "duration_sec": duration_sec,
            }
        )

    except subprocess.TimeoutExpired:
        return json.dumps({"error": "ffmpeg trim timed out"})


# -- ADK FunctionTool wrappers -------------------------------------------------
mux_audio_video_tool = FunctionTool(mux_audio_video)
concat_clips_tool = FunctionTool(concat_clips)
trim_clip_tool = FunctionTool(trim_clip)

assembly_tools = [mux_audio_video_tool, concat_clips_tool, trim_clip_tool]
