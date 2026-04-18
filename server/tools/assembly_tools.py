"""
Assembly tools -- ffmpeg wrappers for muxing, concat, trim, and final
master rendering.

Rules:
- mux_audio_video: NO -shortest flag (video must be >= audio)
- All subprocess calls use list form (no shell=True)
- Final deliverables (filename contains 'final') MUST use a non-preview
  :class:`MasterProfile` — ``PREVIEW_512P`` is rejected by
  :func:`guard_profile_for_filename` unless ``preview_final_ok=True``.
- Upscaling is done here at assembly time via lanczos; LTX clips stay at
  their native low resolution during generation.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import List, Optional

from google.adk.tools import FunctionTool

from .master_profiles import (
    DEFAULT_PROFILE,
    MasterProfile,
    PreviewProfileForbidden,
    guard_profile_for_filename,
)
from .loudness_normalization import (
    LoudnessOutOfSpec,
    normalize_master,
)
from .title_cards import CardSpec, render_card

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
    master_profile: Optional[MasterProfile] = None,
    preview_final_ok: bool = False,
) -> str:
    """Mux audio and video into a single file.

    No -shortest flag: video must be >= audio duration.

    Args:
        audio_path: Path to the audio file (WAV/AAC).
        video_path: Path to the video file (MP4).
        output_path: Path for the muxed output file.
        master_profile: Optional :class:`MasterProfile` — when supplied,
            the encoder is driven off the profile (codec, preset, crf,
            pix_fmt, color space, audio codec/bitrate/sample rate) AND
            the source video is upscaled to ``profile.width`` x
            ``profile.height`` via lanczos during mux.  Downstream LTX
            clips stay at their native low resolution at generation
            time; upscaling only happens here at assembly.
        preview_final_ok: explicit override for the preview-profile
            guard on final-named filenames (maps to ``--preview-final-ok``).

    Returns:
        JSON string with mux result.
    """
    for path, label in [(audio_path, "audio"), (video_path, "video")]:
        if not os.path.exists(path):
            return json.dumps({"error": f"{label} file not found: {path}"})

    if master_profile is not None:
        try:
            guard_profile_for_filename(
                master_profile, output_path, preview_final_ok=preview_final_ok,
            )
        except PreviewProfileForbidden as exc:
            return json.dumps({"error": str(exc)})

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if master_profile is not None:
        # Profile-driven encode: upscale source to the target resolution
        # with lanczos and use the profile's codec / audio settings.
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-vf", master_profile.scale_filter(),
            *master_profile.video_encode_args(),
            *master_profile.audio_encode_args(),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        # Legacy path — kept so scene-level mux (per-phrase) isn't
        # forced to re-encode into full 1080p.
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
    master_profile: Optional[MasterProfile] = None,
    copy_audio: bool = False,
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
        master_profile: Optional :class:`MasterProfile` — when supplied,
            the video re-encode uses the profile's codec / preset / crf /
            pix_fmt / colour tags / fps and the profile's audio codec /
            bitrate / sample rate.  Without it, the legacy H.264 fast
            crf18 + AAC 192k defaults are used (safe for mixed-codec
            inputs from diffusers / transitions).
        copy_audio: When ``True`` (only meaningful with ``master_profile``),
            the audio stream is copied verbatim (``-c:a copy``) instead of
            being re-encoded.  Use this when every input segment already
            has AAC audio at the profile's bitrate / sample rate (e.g. in
            :func:`finalize_master` where all segments come out of the
            same profile-driven mux) — it avoids a redundant lossy AAC
            generation.

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
        elif master_profile is not None:
            # Video + profile: re-encode with the profile's codec settings
            # so the final deliverable keeps its bt709 tags, fps lock,
            # slow preset, and 256k aac.  Without this the hardcoded
            # legacy settings below would silently downgrade quality for
            # every finalize_master() call (see #90).  When copy_audio is
            # True the caller has guaranteed every segment already has
            # matching AAC audio, so we stream-copy it to avoid a third
            # lossy generation (Phase B WAV → mux AAC → concat AAC would
            # otherwise be two AAC transcodes on the same samples).
            audio_args = (
                ["-c:a", "copy"] if copy_audio
                else list(master_profile.audio_encode_args())
            )
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_path,
                *master_profile.video_encode_args(),
                *audio_args,
                "-movflags", "+faststart",
                output_path,
            ]
        else:
            # Video, no profile: re-encode to H.264 + AAC for universal
            # compatibility.  -c copy would be faster but breaks when
            # source codecs differ (e.g. mpeg4 Part 2 from LTX + libx264
            # from black transitions).
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


# ---------------------------------------------------------------------------
# Profile-aware helpers for final master assembly (#90, #91, #104, #105)
# ---------------------------------------------------------------------------

def upscale_to_profile(
    input_path: str,
    output_path: str,
    master_profile: MasterProfile,
    preview_final_ok: bool = False,
) -> str:
    """Re-encode ``input_path`` to the profile's resolution + codec.

    Uses lanczos scaling (documentary-grade) and the profile's H.264
    settings + colour tags.  Preserves the audio stream via ``-c:a copy``
    since audio is normalised separately in Phase B.

    Returns JSON describing the result (status or error).
    """
    if not os.path.exists(input_path):
        return json.dumps({"error": f"Input not found: {input_path}"})

    try:
        guard_profile_for_filename(
            master_profile, output_path, preview_final_ok=preview_final_ok,
        )
    except PreviewProfileForbidden as exc:
        return json.dumps({"error": str(exc)})

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", master_profile.scale_filter(),
        *master_profile.video_encode_args(),
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]

    dur_min = _estimate_file_duration_minutes(input_path)
    timeout = _scaled_timeout(
        _MUX_TIMEOUT_BASE, _MUX_TIMEOUT_PER_MIN, dur_min, _MUX_TIMEOUT_MAX,
    )
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "error": f"upscale timed out after {timeout}s",
        })
    if result.returncode != 0:
        return json.dumps({
            "error": f"upscale failed (rc={result.returncode})",
            "stderr": result.stderr[-500:],
        })
    logger.info(
        "Upscaled %s -> %s (%dx%d, %s)",
        input_path, output_path,
        master_profile.width, master_profile.height, master_profile.name,
    )
    return json.dumps({
        "status": "upscaled",
        "output_path": output_path,
        "profile": master_profile.name,
        "width": master_profile.width,
        "height": master_profile.height,
    })


def finalize_master(
    body_video_path: str,
    body_audio_path: str,
    output_path: str,
    master_profile: MasterProfile = DEFAULT_PROFILE,
    title_card: Optional[CardSpec] = None,
    end_card: Optional[CardSpec] = None,
    preview_final_ok: bool = False,
) -> str:
    """Render a full final deliverable: title → body → end + Phase B loudnorm.

    Steps:
      1. Guard: refuse ``PREVIEW_512P`` for filenames containing ``final``.
      2. Render title/end cards (if supplied) at the profile geometry.
      3. Phase B loudnorm the body audio to the profile envelope.
      4. Mux body audio+video at the profile resolution (lanczos upscale).
      5. Concat title → muxed body → end into ``output_path``.

    All intermediate artifacts are written next to ``output_path`` in a
    ``_final_parts`` sibling directory so they are easy to inspect.

    Returns a JSON string describing the result.
    """
    for path, label in [
        (body_video_path, "body_video"),
        (body_audio_path, "body_audio"),
    ]:
        if not os.path.exists(path):
            return json.dumps({"error": f"{label} not found: {path}"})

    try:
        guard_profile_for_filename(
            master_profile, output_path, preview_final_ok=preview_final_ok,
        )
    except PreviewProfileForbidden as exc:
        return json.dumps({"error": str(exc)})

    out_dir = os.path.dirname(output_path) or "."
    parts_dir = os.path.join(out_dir, "_final_parts")
    os.makedirs(parts_dir, exist_ok=True)

    # 1. Phase B — normalize full narration stream to profile target.
    # Keep the intermediate as lossless PCM WAV so there is exactly one
    # AAC encode in the whole pipeline (the mux step below).  Previously
    # this wrote an .m4a, which meant the body audio went WAV -> AAC
    # (Phase B) -> AAC (mux) -> AAC (concat) — three lossy generations
    # on every deliverable.  See the Devin Review finding on #112.
    normalized_audio = os.path.join(parts_dir, "body_audio_master.wav")
    try:
        normalize_master(
            input_path=body_audio_path,
            output_path=normalized_audio,
            profile=master_profile,
            pcm_intermediate=True,
        )
    except LoudnessOutOfSpec as exc:
        return json.dumps({
            "error": f"Phase B loudnorm out of spec: {exc}",
        })
    except Exception as exc:
        return json.dumps({
            "error": f"Phase B loudnorm failed: {exc}",
        })

    # 2. Mux body at profile resolution (this also upscales via lanczos).
    muxed_body = os.path.join(parts_dir, "body_muxed.mp4")
    mux_result = json.loads(mux_audio_video(
        audio_path=normalized_audio,
        video_path=body_video_path,
        output_path=muxed_body,
        master_profile=master_profile,
        preview_final_ok=preview_final_ok,
    ))
    if "error" in mux_result:
        return json.dumps({"error": f"body mux failed: {mux_result['error']}"})

    segments: List[str] = []

    # 3. Render title card (if any).
    if title_card is not None:
        title_path = os.path.join(parts_dir, "title_card.mp4")
        try:
            render_card(title_card, master_profile, title_path)
        except Exception as exc:
            return json.dumps({"error": f"title card render failed: {exc}"})
        segments.append(title_path)

    segments.append(muxed_body)

    # 4. Render end card (if any).
    if end_card is not None:
        end_path = os.path.join(parts_dir, "end_card.mp4")
        try:
            render_card(end_card, master_profile, end_path)
        except Exception as exc:
            return json.dumps({"error": f"end card render failed: {exc}"})
        segments.append(end_path)

    # 5. Concat everything — if there's nothing to prepend/append, just
    # copy the muxed body to the final output.  All segments were
    # rendered with the same master_profile, so we pass it through to
    # concat_clips so the final re-encode keeps the profile's codec /
    # bitrate / colour tags / fps rather than the legacy defaults.
    if len(segments) == 1:
        shutil.copy2(segments[0], output_path)
    else:
        concat_result = json.loads(concat_clips(
            clip_paths=",".join(segments),
            output_path=output_path,
            master_profile=master_profile,
            # All segments (title card, muxed body, end card) had their
            # audio encoded via the same profile.audio_encode_args(), so
            # AudioSpecificConfig is identical across them and we can
            # stream-copy audio without a redundant lossy AAC pass.
            copy_audio=True,
        ))
        if "error" in concat_result:
            return json.dumps({
                "error": f"final concat failed: {concat_result['error']}",
            })

    logger.info(
        "Finalized master %s (%s, title=%s, end=%s)",
        output_path, master_profile.name,
        bool(title_card), bool(end_card),
    )
    return json.dumps({
        "status": "finalized",
        "output_path": output_path,
        "profile": master_profile.name,
        "has_title_card": title_card is not None,
        "has_end_card": end_card is not None,
    })


# -- ADK FunctionTool wrappers -------------------------------------------------
mux_audio_video_tool = FunctionTool(mux_audio_video)
concat_clips_tool = FunctionTool(concat_clips)
trim_clip_tool = FunctionTool(trim_clip)

assembly_tools = [mux_audio_video_tool, concat_clips_tool, trim_clip_tool]

__all__ = [
    "normalize_audio_loudness",
    "mux_audio_video",
    "concat_clips",
    "trim_clip",
    "upscale_to_profile",
    "finalize_master",
    "assembly_tools",
]
