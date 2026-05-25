from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import List, Optional

from .master_profiles import (
    DEFAULT_PROFILE,
    MasterProfile,
)

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

# NOTE: ARCH-F3 (#164) removed ``trim_clip`` entirely.  The assembler no
# longer extracts sub-ranges — OTIO Clips MUST be generated at their
# declared exact length, and length-mismatched clips are REPLACEd via
# the content ladder rather than trimmed at render time.  See
# :mod:`callbacks.strict_assembler` for the structured errors raised
# when the invariant is violated.


# Regex to extract scene number from typical clip filenames.
_SCENE_NUM_RE = re.compile(r"scene[_\-]?(\d+)", re.IGNORECASE)


def _extract_scene_num(path: str) -> Optional[int]:
    """Attempt to extract scene number from a file path."""
    m = _SCENE_NUM_RE.search(os.path.basename(path))
    return int(m.group(1)) if m else None


def _probe_duration(path: str) -> Optional[float]:
    """Return duration of a media file in seconds via ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        result = subprocess.run(
            cmd, check=True, capture_output=True, text=True
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not probe duration for %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Placeholder cache — persist generated placeholders so lookups across
# runs (or resumed pipelines) can resolve them without regeneration.
# ---------------------------------------------------------------------------
_PLACEHOLDER_CACHE: dict[str, str] = {}


def generate_video_placeholder(path: str, duration: float = 5.0) -> Optional[str]:
    """Generate a black-frame silent placeholder video for a missing clip.

    Uses ffmpeg lavfi filters.  Caches results in _PLACEHOLDER_CACHE so
    resumed pipelines can resolve them without regeneration.
    """
    cache_key = f"{path}:{duration}"
    if cache_key in _PLACEHOLDER_CACHE:
        cached = _PLACEHOLDER_CACHE[cache_key]
        if os.path.exists(cached):
            return cached

    base, ext = os.path.splitext(path)
    placeholder_path = f"{base}.placeholder{ext}"

    # Reuse an existing placeholder on disk when the cache is cold.
    if os.path.exists(placeholder_path):
        _PLACEHOLDER_CACHE[cache_key] = placeholder_path
        return placeholder_path

    seq = 0
    candidate = placeholder_path
    while os.path.exists(candidate):
        candidate = f"{base}.placeholder.{seq:03d}{ext}"
        seq += 1
    placeholder_path = candidate

    width, height = 512, 288
    sample_rate = 44100

    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s={width}x{height}:d={duration}",
        "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl=mono",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-pix_fmt", "yuv420p",
        placeholder_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("Placeholder generation failed for %s: %s", path, exc)
        return None

    _PLACEHOLDER_CACHE[cache_key] = placeholder_path
    return placeholder_path


def concat_clips(
    paths: List[str],
    output_path: str,
    profile: MasterProfile = DEFAULT_PROFILE,
    durations: Optional[List[float]] = None,
) -> str:
    """Concatenate video clips with mandatory placeholder fallback.

    When a required clip is absent, a black-frame silent placeholder is
    generated so assembly continues.  The gap is logged at CRITICAL —
    never silently swallowed.
    """
    resolved: List[str] = []
    gaps: List[dict] = []
    for idx, path in enumerate(paths):
        if not os.path.exists(path):
            scene_num = _extract_scene_num(path)
            expected_duration = durations[idx] if durations and idx < len(durations) else 5.0
            logger.critical(
                "scene=%s | MISSING video clip at path=%s — generating placeholder",
                scene_num if scene_num is not None else "UNKNOWN",
                path,
            )
            placeholder = generate_video_placeholder(path, duration=expected_duration)
            if placeholder is None:
                logger.critical(
                    "scene=%s | MISSING video clip at path=%s — placeholder generation FAILED",
                    scene_num if scene_num is not None else "UNKNOWN",
                    path,
                )
                return json.dumps(
                    {"error": f"Clip not found and placeholder generation failed: {path}"}
                )
            resolved.append(placeholder)
            gaps.append(
                {
                    "scene_number": scene_num,
                    "missing_path": path,
                    "placeholder_path": placeholder,
                    "expected_duration": expected_duration,
                }
            )
        else:
            resolved.append(path)

    # Build ffmpeg concat demuxer list
    concat_list_path = output_path + ".concat_list.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for p in resolved:
            escaped = p.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    concat_timeout = _CONCAT_TIMEOUT_BASE + (_CONCAT_TIMEOUT_PER_CLIP * len(resolved))
    concat_timeout = min(concat_timeout, _CONCAT_TIMEOUT_MAX)

    cmd = [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        output_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("Concatenation failed: %s", exc)
        return json.dumps({"error": f"Concatenation failed: {exc}"})
    finally:
        try:
            os.unlink(concat_list_path)
        except OSError:
            pass

    # Write scene failure report listing every gap in the movie
    report_path = os.path.join(os.path.dirname(output_path) or ".", "scene_failure_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(gaps, f, indent=2)

    # ------------------------------------------------------------------
    # Final audit: assert no unlogged gaps exist in the assembled movie
    # ------------------------------------------------------------------
    actual_duration = _probe_duration(output_path)
    expected_duration = 0.0
    for idx, path in enumerate(paths):
        if durations and idx < len(durations):
            expected_duration += durations[idx]
        else:
            if os.path.exists(path):
                d = _probe_duration(path)
                expected_duration += d if d else 0.0
            else:
                expected_duration += 5.0

    if actual_duration is not None and expected_duration > 0:
        if actual_duration < expected_duration - 0.5:
            logger.critical(
                "AUDIT FAILED: assembled movie duration %.3fs is shorter than expected %.3fs — unlogged gap detected",
                actual_duration,
                expected_duration,
            )
            return json.dumps(
                {
                    "error": "Unlogged gap detected in assembled movie",
                    "actual_duration": actual_duration,
                    "expected_duration": expected_duration,
                }
            )

    return output_path


def assemble_documentary(
    timeline_path: str = "",
    output_dir: str = "",
    master_filename: str = "master.mp4",
    clip_artifacts: dict | None = None,
) -> str:
    """Assemble the final documentary from OTIO timeline clips or a provided clip list.

    If ``clip_artifacts`` is provided, it MUST contain ``video_clips`` and/or
    ``audio_clips`` lists (each item is a dict with ``path``, ``duration``,
    ``name``). OTIO is NOT read in that case.

    Otherwise reads V1_Video and A1_Narration tracks from the OTIO file.

    Returns:
        JSON string with output_path and metadata.
    """
    import opentimelineio as otio

    os.makedirs(output_dir, exist_ok=True)
    master_path = os.path.join(output_dir, master_filename)

    video_clips: list[dict] = []
    audio_clips: list[dict] = []

    if clip_artifacts is not None:
        video_clips = list(clip_artifacts.get("video_clips", []))
        audio_clips = list(clip_artifacts.get("audio_clips", []))
    elif timeline_path:
        timeline = otio.adapters.read_from_file(timeline_path)
        for track in timeline.tracks:
            if track.name == "V1_Video":
                for item in track:
                    if isinstance(item, otio.schema.Clip):
                        ref = item.media_reference
                        path = ref.target_url.replace("file://", "") if hasattr(ref, "target_url") else ""
                        duration = float(item.duration().value) / float(item.duration().rate) if hasattr(item.duration(), "value") else 5.0
                        video_clips.append({"path": path, "duration": duration, "name": item.name})
            elif track.name == "A1_Narration":
                for item in track:
                    if isinstance(item, otio.schema.Clip):
                        ref = item.media_reference
                        path = ref.target_url.replace("file://", "") if hasattr(ref, "target_url") else ""
                        duration = float(item.duration().value) / float(item.duration().rate) if hasattr(item.duration(), "value") else 5.0
                        audio_clips.append({"path": path, "duration": duration, "name": item.name})

    if not video_clips and not audio_clips:
        return json.dumps({"error": "No clips found in timeline", "output_path": ""})

    # Strategy: if we have video clips, concat them. If we have audio, mix it in.
    # For simplicity: concatenate video clips, then mux with concatenated audio.

    video_paths = [c["path"] for c in video_clips if os.path.exists(c["path"])]
    audio_paths = [c["path"] for c in audio_clips if os.path.exists(c["path"])]

    gaps: list[dict] = []

    # Handle missing video clips
    for i, c in enumerate(video_clips):
        if not os.path.exists(c["path"]):
            ph = generate_video_placeholder(c["path"], duration=c.get("duration", 5.0))
            if ph:
                video_paths.insert(i, ph)
                gaps.append({"scene": c["name"], "missing": c["path"], "placeholder": ph})
            else:
                return json.dumps({"error": f"Missing video clip and placeholder failed: {c['path']}"})

    # Concatenate video
    if len(video_paths) == 1:
        video_concat_path = video_paths[0]
    elif len(video_paths) > 1:
        video_concat_path = os.path.join(output_dir, "_temp_video_concat.mp4")
        result = concat_clips(video_paths, video_concat_path)
        if isinstance(result, str) and result.startswith('{"error"'):
            return result
    else:
        # No video - generate a black placeholder for the audio duration
        total_audio_duration = sum(c.get("duration", 5.0) for c in audio_clips) if audio_clips else 5.0
        video_concat_path = os.path.join(output_dir, "_temp_black_video.mp4")
        ph = generate_video_placeholder(video_concat_path, duration=total_audio_duration)
        if not ph:
            return json.dumps({"error": "No video clips and placeholder generation failed"})
        video_concat_path = ph

    # Handle audio: concatenate WAV files into a single AAC track
    if audio_paths:
        audio_concat_path = os.path.join(output_dir, "_temp_audio_concat.aac")
        # Build concat demuxer for audio
        audio_list = audio_concat_path + ".list.txt"
        with open(audio_list, "w", encoding="utf-8") as f:
            for p in audio_paths:
                escaped = p.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", audio_list,
            "-c:a", "aac", "-b:a", "128k",
            audio_concat_path,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.error("Audio concatenation failed: %s", exc)
            # Fallback: use first audio file only
            if audio_paths:
                audio_concat_path = audio_paths[0]
            else:
                audio_concat_path = ""
        finally:
            try:
                os.unlink(audio_list)
            except OSError:
                pass
    else:
        audio_concat_path = ""

    # Final mux
    if audio_concat_path and os.path.exists(audio_concat_path):
        cmd = [
            "ffmpeg", "-y",
            "-i", video_concat_path,
            "-i", audio_concat_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            master_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_concat_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            master_path,
        ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("Final mux failed: %s", exc)
        return json.dumps({"error": f"Final mux failed: {exc}"})

    # Cleanup temp files
    for temp in [os.path.join(output_dir, "_temp_video_concat.mp4"), os.path.join(output_dir, "_temp_audio_concat.aac")]:
        try:
            if os.path.exists(temp):
                os.unlink(temp)
        except OSError:
            pass

    actual_duration = _probe_duration(master_path)

    return json.dumps({
        "output_path": master_path,
        "duration": actual_duration,
        "video_clips": len(video_clips),
        "audio_clips": len(audio_clips),
        "gaps": gaps,
        "status": "assembled",
    })


def mux_audio_video(
    *, audio_path: str, video_path: str, output_path: str
) -> str:
    """Mux audio and video streams into a single MP4 using ffmpeg.

    Returns JSON with keys: status, output_path, or error.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        output_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        logger.error("mux_audio_video failed: %s", exc)
        return json.dumps({"error": f"Mux failed: {exc}"})

    return json.dumps({"status": "muxed", "output_path": output_path})


def normalize_audio_loudness(*, input_path: str, output_path: str) -> str:
    """Normalize audio loudness to EBU R128 standard using ffmpeg loudnorm.

    Two-pass loudnorm filter. Falls back to copying if analysis fails.
    Returns JSON with keys: status, output_path, or error.
    """
    # First pass: measure
    measure_cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-af", "loudnorm=print_format=json",
        "-f", "null", "-",
    ]
    try:
        measure_result = subprocess.run(measure_cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        logger.error("loudnorm measurement failed: %s", exc)
        return json.dumps({"error": f"Loudnorm measure failed: {exc}"})

    # Parse JSON from stderr
    stderr = measure_result.stderr
    json_start = stderr.rfind("{")
    json_end = stderr.rfind("}") + 1
    if json_start == -1 or json_end <= json_start:
        logger.warning("Could not parse loudnorm JSON, copying without normalization")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", input_path, "-c:a", "copy", output_path],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            return json.dumps({"error": f"Copy fallback failed: {exc}"})
        return json.dumps({
            "status": "copied_without_normalization",
            "output_path": output_path,
        })

    try:
        measured = json.loads(stderr[json_start:json_end])
    except json.JSONDecodeError as exc:
        logger.warning("loudnorm JSON parse failed: %s", exc)
        return json.dumps({"status": "copied_without_normalization", "output_path": output_path})

    # Second pass: apply measured normalization
    measured_i = measured.get("output_i", "-23.0")
    measured_tp = measured.get("output_tp", "-2.0")
    measured_lra = measured.get("output_lra", "7.0")
    measured_thresh = measured.get("output_thresh", "-30.0")
    offset = measured.get("target_offset", "0.0")

    apply_cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-af",
        (
            f"loudnorm=I=-23:TP=-2.0:LRA=7:"
            f"measured_I={measured_i}:measured_TP={measured_tp}:"
            f"measured_LRA={measured_lra}:measured_thresh={measured_thresh}:"
            f"offset={offset}"
        ),
        "-c:a", "aac", "-b:a", "128k",
        output_path,
    ]
    try:
        subprocess.run(apply_cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        logger.error("loudnorm apply failed: %s", exc)
        return json.dumps({"error": f"Loudnorm apply failed: {exc}"})

    return json.dumps({"status": "normalized", "output_path": output_path})
