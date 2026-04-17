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

# ---------------------------------------------------------------------------
# Timeout scaling — learned from Strands migration
# ---------------------------------------------------------------------------
# Hardcoded 120s/300s timeouts are too short for 5-minute movies (30+ clips).
# A 5-min H.264 re-encode at 'fast' preset takes ~10s per minute of content
# on a modern CPU; concat of 30+ clips can take 5-10 minutes.
# Formula: base + (per_minute * estimated_duration_minutes), clamped to a max.

_MUX_TIMEOUT_BASE = 60       # seconds base
_MUX_TIMEOUT_PER_MIN = 30    # seconds per minute of input
_MUX_TIMEOUT_MAX = 1200      # 20-minute hard cap

_CONCAT_TIMEOUT_BASE = 60
_CONCAT_TIMEOUT_PER_CLIP = 20  # seconds per clip (re-encode overhead)
_CONCAT_TIMEOUT_MAX = 1800     # 30-minute hard cap

_TRIM_TIMEOUT_BASE = 30
_TRIM_TIMEOUT_PER_MIN = 15
_TRIM_TIMEOUT_MAX = 600


def _estimate_file_duration_minutes(path: str) -> float:
    """Estimate media duration in minutes via ffprobe (fast, metadata only).

    Returns 5.0 as a safe default if ffprobe fails.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries",
             "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip()) / 60.0
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return 5.0  # conservative default for a 5-min movie


def _scaled_timeout(base: int, per_unit: int, units: float, cap: int) -> int:
    """Calculate a scaled timeout clamped to a maximum."""
    return min(cap, int(base + per_unit * units))


def normalize_audio_loudness(
    input_path: str,
    output_path: str,
    target_lufs: float = -16.0,
    tool_context=None,
) -> str:
    """Normalize audio loudness using ffmpeg's loudnorm filter (EBU R128).

    Different TTS voices produce clips at varying volume levels.  Without
    normalization, the final documentary has jarring volume shifts between
    narrators (V1/V2/V3).  This two-pass loudnorm ensures consistent
    perceived loudness across all narration clips.

    Args:
        input_path: Path to the input audio file (WAV).
        output_path: Path for the normalized output file.
        target_lufs: Target integrated loudness in LUFS (default -16.0,
            broadcast standard for web content).

    Returns:
        JSON string with normalization result.
    """
    if not os.path.exists(input_path):
        return json.dumps({"error": f"Input file not found: {input_path}"})

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # Pass 1: measure loudness stats
    measure_cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    ]

    try:
        measure_result = subprocess.run(
            measure_cmd, capture_output=True, text=True, timeout=120,
        )
        if measure_result.returncode != 0:
            logger.warning(
                "Loudnorm pass 1 failed (rc=%d), copying without normalization",
                measure_result.returncode,
            )
            # Fallback: copy without normalization rather than failing
            import shutil
            shutil.copy2(input_path, output_path)
            return json.dumps({
                "status": "copied_without_normalization",
                "output_path": output_path,
                "reason": "loudnorm measurement failed",
            })

        # Extract measured loudness from stderr (ffmpeg writes stats there)
        stderr = measure_result.stderr
        # Find the JSON block in stderr
        json_start = stderr.rfind("{")
        json_end = stderr.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            stats = json.loads(stderr[json_start:json_end])
        else:
            logger.warning("Could not parse loudnorm stats, using single-pass")
            stats = None

    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("Loudnorm pass 1 error: %s, using single-pass", exc)
        stats = None

    # Pass 2: apply normalization with measured stats (or single-pass fallback)
    if stats:
        measured_i = stats.get("input_i", "-24.0")
        measured_tp = stats.get("input_tp", "-2.0")
        measured_lra = stats.get("input_lra", "7.0")
        measured_thresh = stats.get("input_thresh", "-34.0")
        af_filter = (
            f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
            f"measured_I={measured_i}:measured_TP={measured_tp}:"
            f"measured_LRA={measured_lra}:measured_thresh={measured_thresh}:"
            f"linear=true"
        )
    else:
        af_filter = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"

    normalize_cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-af", af_filter,
        "-ar", "24000",  # match TTS output sample rate
        output_path,
    ]

    dur_min = _estimate_file_duration_minutes(input_path)
    timeout = _scaled_timeout(60, 20, dur_min, 600)

    try:
        result = subprocess.run(
            normalize_cmd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning(
                "Loudnorm pass 2 failed (rc=%d), copying without normalization",
                result.returncode,
            )
            import shutil
            shutil.copy2(input_path, output_path)
            return json.dumps({
                "status": "copied_without_normalization",
                "output_path": output_path,
                "reason": f"loudnorm encoding failed (rc={result.returncode})",
            })

        logger.info(
            "Normalized %s -> %s (target=%.1f LUFS)",
            input_path, output_path, target_lufs,
        )
        return json.dumps({
            "status": "normalized",
            "output_path": output_path,
            "target_lufs": target_lufs,
            "measured_lufs": stats.get("input_i") if stats else "unknown",
        })

    except subprocess.TimeoutExpired:
        logger.warning("Loudnorm timed out after %ds, copying without normalization", timeout)
        import shutil
        shutil.copy2(input_path, output_path)
        return json.dumps({
            "status": "copied_without_normalization",
            "output_path": output_path,
            "reason": f"loudnorm timed out after {timeout}s",
        })


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

    # Scale timeout by input duration (a 5-min movie re-encode takes much
    # longer than the old hardcoded 120s allowed)
    dur_min = max(
        _estimate_file_duration_minutes(audio_path),
        _estimate_file_duration_minutes(video_path),
    )
    timeout = _scaled_timeout(
        _MUX_TIMEOUT_BASE, _MUX_TIMEOUT_PER_MIN, dur_min, _MUX_TIMEOUT_MAX,
    )
    logger.info(
        "ffmpeg mux: estimated %.1f min input, timeout=%ds", dur_min, timeout,
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
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
        return json.dumps({"error": f"ffmpeg mux timed out after {timeout}s (input ~{dur_min:.1f} min)"})


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

        # Scale timeout by number of clips (re-encoding 30+ clips takes
        # much longer than the old hardcoded 300s)
        timeout = _scaled_timeout(
            _CONCAT_TIMEOUT_BASE, _CONCAT_TIMEOUT_PER_CLIP,
            len(paths), _CONCAT_TIMEOUT_MAX,
        )
        logger.info(
            "ffmpeg concat: %d clips, timeout=%ds", len(paths), timeout,
        )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
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
        return json.dumps({"error": f"ffmpeg concat timed out after {timeout}s ({len(paths)} clips)"})
    finally:
        try:
            os.unlink(concat_path)
        except OSError:
            pass


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

    # Scale timeout by duration being extracted
    dur_min = duration_sec / 60.0
    timeout = _scaled_timeout(
        _TRIM_TIMEOUT_BASE, _TRIM_TIMEOUT_PER_MIN, dur_min, _TRIM_TIMEOUT_MAX,
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
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
        return json.dumps({"error": f"ffmpeg trim timed out after {timeout}s"})


# -- ADK FunctionTool wrappers -------------------------------------------------
mux_audio_video_tool = FunctionTool(mux_audio_video)
concat_clips_tool = FunctionTool(concat_clips)
trim_clip_tool = FunctionTool(trim_clip)

assembly_tools = [mux_audio_video_tool, concat_clips_tool, trim_clip_tool]
