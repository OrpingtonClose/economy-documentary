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

from strands import tool

logger = logging.getLogger(__name__)


@tool
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

    # Re-encode video to H.264 during mux.  Source clips from
    # diffusers' export_to_video use mpeg4 Part 2 which most players
    # cannot render.  Re-encoding here normalises everything to H.264
    # before concat, eliminating mixed-codec glitches.
    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-movflags", "+faststart",
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


@tool
def concat_clips(
    clip_paths: str,
    output_path: str,
    tool_context=None,
) -> str:
    """Concatenate a list of clips using ffmpeg concat demuxer.

    For video files: re-encodes to H.264 (libx264) + AAC to ensure
    universal player compatibility.  Source clips may use different
    codecs (e.g. mpeg4 Part 2 from diffusers vs. libx264 from black
    transitions), and concat demuxer with -c copy produces broken
    output when codecs are mixed.

    For audio-only files (WAV): uses -c copy (stream copy) since WAV
    containers don't support AAC and all narration clips are already
    PCM WAV.

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
    concat_fd, concat_path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(concat_fd, "w") as concat_file:
            for path in paths:
                concat_file.write(f"file '{path}'\n")

        # Detect audio-only mode: if output is WAV (or all inputs are
        # audio-only), use stream copy to avoid re-encoding to AAC
        # which is incompatible with WAV containers.
        _audio_only = output_path.lower().endswith(".wav") or all(
            p.lower().endswith((".wav", ".flac", ".ogg", ".mp3"))
            for p in paths
        )

        if _audio_only:
            # Audio-only: stream copy (all narration clips are PCM WAV)
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_path,
                "-c", "copy",
                output_path,
            ]
        else:
            # Video: re-encode to H.264 + AAC for universal compatibility.
            # -c copy would be faster but breaks when source codecs differ
            # (e.g. mpeg4 Part 2 from LTX + libx264 from black transitions).
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_path,
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "18",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
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

        logger.info("Concatenated %d clips -> %s (H.264)", len(paths), output_path)
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
        try:
            os.unlink(concat_path)
        except OSError:
            pass


@tool
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


assembly_tools = [mux_audio_video, concat_clips, trim_clip]
