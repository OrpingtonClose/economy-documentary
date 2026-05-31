"""Deterministic QA gates for v4 pipeline.

Pixel- and ffprobe-level quality gates that run on every artifact
before assembly.  No LLM required — pure ffmpeg/numpy.

Gates:
- qa_video_artifact_probe  — ffprobe duration / dimensions / codec / frames
- qa_duration_align        — audio vs video duration sanity
- qa_stills_judge          — detect frozen frames via inter-frame delta
- qa_audio_completeness    — detect abrupt audio cuts via silence + RMS
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

VERDICT_PASS: str = "pass"
VERDICT_FAIL: str = "fail"

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

DEFAULT_DURATION_TOLERANCE_S: float = 0.5
DEFAULT_MAX_LOOP_FACTOR: float = 5.0
MIN_VIDEO_DURATION_S: float = 0.5
MIN_VIDEO_BYTES: int = 1024
DEFAULT_STILLS_NUM_SAMPLES: int = 8
DEFAULT_MIN_MEAN_PIXEL_DELTA: float = 1.5
MIN_TRAILING_SILENCE_S: float = 0.15
SILENCE_NOISE_DB: float = -40.0
MIN_NARRATION_DURATION_S: float = 0.5


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _ProbeError(RuntimeError):
    pass


def _ffprobe_path() -> str:
    found = shutil.which("ffprobe")
    if not found:
        raise _ProbeError("ffprobe not found on PATH")
    return found


def _ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise _ProbeError("ffmpeg not found on PATH")
    return found


def _ffprobe_json(media_path: Path) -> dict[str, Any]:
    cmd = [
        _ffprobe_path(),
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-of", "json",
        str(media_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise _ProbeError(
            f"ffprobe failed for {media_path}: rc={result.returncode} "
            f"stderr={result.stderr.strip()[:200]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise _ProbeError(
            f"ffprobe emitted non-JSON for {media_path}: {result.stdout[:200]}"
        ) from exc


def _resolve_duration_s(payload: dict[str, Any]) -> float | None:
    fmt = payload.get("format") or {}
    raw = fmt.get("duration")
    if raw is None:
        for stream in payload.get("streams") or []:
            if stream.get("duration") is not None:
                raw = stream["duration"]
                break
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _video_stream(payload: dict[str, Any]) -> dict[str, Any] | None:
    for stream in payload.get("streams") or []:
        if stream.get("codec_type") == "video":
            return stream
    return None


def _decode_pgm(data: bytes) -> tuple[int, int, bytes]:
    buf = io.BytesIO(data)
    magic = buf.readline().strip()
    if magic != b"P5":
        raise _ProbeError(f"unexpected PGM magic: {magic!r}")
    line = buf.readline()
    while line.startswith(b"#"):
        line = buf.readline()
    try:
        width_str, height_str = line.strip().split()
        width = int(width_str)
        height = int(height_str)
    except ValueError as exc:
        raise _ProbeError(f"unparseable PGM dimensions: {line!r}") from exc
    maxval_line = buf.readline()
    try:
        maxval = int(maxval_line.strip())
    except ValueError as exc:
        raise _ProbeError(f"unparseable PGM maxval: {maxval_line!r}") from exc
    if maxval != 255:
        raise _ProbeError(f"unsupported PGM maxval (need 255): {maxval}")
    pixels = buf.read(width * height)
    if len(pixels) != width * height:
        raise _ProbeError(
            f"PGM payload length {len(pixels)} != w*h {width * height}"
        )
    return width, height, pixels


def _extract_grey_frames(
    video_path: Path,
    num_samples: int,
    *,
    width: int = 64,
    height: int = 64,
) -> list[bytes]:
    payload = _ffprobe_json(video_path)
    duration = _resolve_duration_s(payload)
    if duration is None or duration <= 0:
        raise _ProbeError(f"video {video_path} has no usable duration")
    samples: list[bytes] = []
    for i in range(num_samples):
        t = duration * (i + 0.5) / num_samples
        cmd = [
            _ffmpeg_path(),
            "-loglevel", "error",
            "-ss", f"{t:.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-vf", f"scale={width}:{height},format=gray",
            "-c:v", "pgm",
            "-f", "image2pipe",
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise _ProbeError(
                f"ffmpeg sample at t={t:.3f}s failed for {video_path}: "
                f"stderr={result.stderr.decode('utf-8', 'replace')[:200]}"
            )
        _, _, pixels = _decode_pgm(result.stdout)
        samples.append(pixels)
    return samples


def _mean_l1_delta(frames: list[bytes]) -> float:
    if len(frames) < 2:
        return 0.0
    deltas: list[float] = []
    for prev, curr in zip(frames, frames[1:]):
        if len(prev) != len(curr) or not prev:
            continue
        total = 0
        for a, b in zip(prev, curr):
            total += a - b if a > b else b - a
        deltas.append(total / len(curr))
    if not deltas:
        return 0.0
    return sum(deltas) / len(deltas)


def _envelope(
    tool_name: str,
    *,
    scene_id: str,
    verdict: str,
    **fields: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "tool": tool_name,
        "scene_id": scene_id,
        "verdict": verdict,
    }
    out.update(fields)
    return out


def _detect_silence_segments(
    audio_path: Path,
    *,
    noise_db: float = SILENCE_NOISE_DB,
    min_duration_s: float = 0.05,
) -> list[tuple[float, float]]:
    cmd = [
        _ffmpeg_path(),
        "-hide_banner",
        "-nostats",
        "-i", str(audio_path),
        "-af", f"silencedetect=noise={noise_db}dB:duration={min_duration_s}",
        "-f", "null",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise _ProbeError(
            f"ffmpeg silencedetect failed for {audio_path}: rc={result.returncode} "
            f"stderr={result.stderr.strip()[:200]}"
        )
    starts: list[float] = []
    ends: list[float] = []
    for raw_line in result.stderr.splitlines():
        line = raw_line.strip()
        if "silence_start:" in line:
            try:
                starts.append(float(line.rsplit("silence_start:", 1)[1].strip()))
            except ValueError:
                continue
        elif "silence_end:" in line:
            tail = line.rsplit("silence_end:", 1)[1].strip()
            try:
                ends.append(float(tail.split()[0]))
            except (ValueError, IndexError):
                continue
    paired: list[tuple[float, float]] = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else float("nan")
        paired.append((start, end))
    return paired


def _measure_tail_rms_db(
    audio_path: Path,
    *,
    duration_s: float,
    tail_window_s: float = 0.05,
) -> float:
    if duration_s <= tail_window_s:
        return float("-inf")
    seek = max(0.0, duration_s - tail_window_s)
    cmd = [
        _ffmpeg_path(),
        "-hide_banner",
        "-nostats",
        "-ss", f"{seek:.6f}",
        "-i", str(audio_path),
        "-t", f"{tail_window_s:.6f}",
        "-af", "astats=metadata=1:reset=0",
        "-f", "null",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise _ProbeError(
            f"ffmpeg astats failed for {audio_path}: rc={result.returncode} "
            f"stderr={result.stderr.strip()[:200]}"
        )
    rms_values: list[float] = []
    for raw_line in result.stderr.splitlines():
        line = raw_line.strip()
        if "RMS level dB:" in line:
            tail = line.rsplit("RMS level dB:", 1)[1].strip()
        elif "RMS_level=" in line:
            tail = line.rsplit("RMS_level=", 1)[1].strip()
        else:
            continue
        try:
            rms_values.append(float(tail.split()[0]))
        except (ValueError, IndexError):
            continue
    if not rms_values:
        return float("-inf")
    finite = [v for v in rms_values if v != float("-inf") and v == v]
    if not finite:
        return float("-inf")
    return sum(finite) / len(finite)


# ---------------------------------------------------------------------------
# Public gates
# ---------------------------------------------------------------------------

def qa_video_artifact_probe(scene_id: str, video_path: str) -> dict[str, Any]:
    """Probe a rendered MP4 with ffprobe and return its key facts."""
    path = Path(video_path)
    if not path.is_file():
        return _envelope(
            "qa_video_artifact_probe",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(path),
            error=f"video_path does not exist: {path}",
        )
    size = path.stat().st_size
    if size < MIN_VIDEO_BYTES:
        return _envelope(
            "qa_video_artifact_probe",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(path),
            size_bytes=size,
            error=f"video too small ({size} < {MIN_VIDEO_BYTES})",
        )
    try:
        payload = _ffprobe_json(path)
    except _ProbeError as exc:
        return _envelope(
            "qa_video_artifact_probe",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(path),
            size_bytes=size,
            error=str(exc),
        )
    duration_s = _resolve_duration_s(payload)
    stream = _video_stream(payload)
    if stream is None:
        return _envelope(
            "qa_video_artifact_probe",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(path),
            size_bytes=size,
            duration_s=duration_s,
            error="no video stream in artifact",
        )
    nb_frames_raw = stream.get("nb_frames")
    try:
        nb_frames = int(nb_frames_raw) if nb_frames_raw is not None else None
    except (TypeError, ValueError):
        nb_frames = None
    return _envelope(
        "qa_video_artifact_probe",
        scene_id=scene_id,
        verdict=VERDICT_PASS,
        video_path=str(path),
        size_bytes=size,
        duration_s=duration_s,
        width=stream.get("width"),
        height=stream.get("height"),
        codec=stream.get("codec_name"),
        nb_frames=nb_frames,
    )


def qa_duration_align(
    scene_id: str,
    audio_path: str,
    video_path: str,
    tolerance_s: float = DEFAULT_DURATION_TOLERANCE_S,
    max_loop_factor: float = DEFAULT_MAX_LOOP_FACTOR,
    min_video_duration_s: float = MIN_VIDEO_DURATION_S,
) -> dict[str, Any]:
    """Hard-fail when a scene's video can't be cleanly muxed against audio."""
    audio = Path(audio_path)
    video = Path(video_path)
    if not audio.is_file():
        return _envelope(
            "qa_duration_align",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            video_path=str(video),
            error=f"audio_path does not exist: {audio}",
        )
    if not video.is_file():
        return _envelope(
            "qa_duration_align",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            video_path=str(video),
            error=f"video_path does not exist: {video}",
        )
    try:
        audio_payload = _ffprobe_json(audio)
        video_payload = _ffprobe_json(video)
    except _ProbeError as exc:
        return _envelope(
            "qa_duration_align",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            video_path=str(video),
            error=str(exc),
        )
    audio_dur = _resolve_duration_s(audio_payload)
    video_dur = _resolve_duration_s(video_payload)
    if audio_dur is None or video_dur is None:
        return _envelope(
            "qa_duration_align",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            video_path=str(video),
            audio_duration_s=audio_dur,
            video_duration_s=video_dur,
            error="ffprobe returned no duration on at least one artifact",
        )
    pre_mux_delta_s = abs(audio_dur - video_dur)
    loop_factor = audio_dur / video_dur if video_dur > 0 else float("inf")
    fail_reasons: list[str] = []
    if video_dur < float(min_video_duration_s):
        fail_reasons.append(
            f"video_dur = {video_dur:.3f} s < min_video_duration_s {float(min_video_duration_s):.3f} s"
        )
    if loop_factor > float(max_loop_factor):
        fail_reasons.append(
            f"loop_factor = {loop_factor:.2f}x > max_loop_factor {float(max_loop_factor):.2f}x"
        )
    verdict = VERDICT_PASS if not fail_reasons else VERDICT_FAIL
    out = _envelope(
        "qa_duration_align",
        scene_id=scene_id,
        verdict=verdict,
        audio_path=str(audio),
        video_path=str(video),
        audio_duration_s=audio_dur,
        video_duration_s=video_dur,
        pre_mux_delta_s=pre_mux_delta_s,
        loop_factor=loop_factor,
    )
    if verdict == VERDICT_FAIL:
        out["reason"] = "; ".join(fail_reasons)
    return out


def qa_stills_judge(
    scene_id: str,
    video_path: str,
    num_samples: int = DEFAULT_STILLS_NUM_SAMPLES,
    min_mean_pixel_delta: float = DEFAULT_MIN_MEAN_PIXEL_DELTA,
) -> dict[str, Any]:
    """Hard-fail when a rendered video has no meaningful motion."""
    if num_samples < 2:
        raise ValueError("num_samples must be >= 2")
    video = Path(video_path)
    if not video.is_file():
        return _envelope(
            "qa_stills_judge",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(video),
            error=f"video_path does not exist: {video}",
        )
    try:
        frames = _extract_grey_frames(video, int(num_samples))
    except _ProbeError as exc:
        return _envelope(
            "qa_stills_judge",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(video),
            error=str(exc),
        )
    mean_delta = _mean_l1_delta(frames)
    verdict = VERDICT_PASS if mean_delta >= float(min_mean_pixel_delta) else VERDICT_FAIL
    out = _envelope(
        "qa_stills_judge",
        scene_id=scene_id,
        verdict=verdict,
        video_path=str(video),
        num_samples=int(num_samples),
        mean_pixel_delta=mean_delta,
    )
    if verdict == VERDICT_FAIL:
        out["reason"] = (
            f"mean inter-frame pixel delta {mean_delta:.3f} below "
            f"floor {float(min_mean_pixel_delta):.3f}"
        )
    return out


def qa_audio_completeness(
    scene_id: str,
    audio_path: str,
    min_trailing_silence_s: float = MIN_TRAILING_SILENCE_S,
    silence_noise_db: float = SILENCE_NOISE_DB,
    max_tail_rms_db: float = -25.0,
    tail_window_s: float = 0.05,
) -> dict[str, Any]:
    """Hard-fail when narration ends abruptly mid-utterance."""
    audio = Path(audio_path)
    if not audio.is_file():
        return _envelope(
            "qa_audio_completeness",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            error=f"audio_path does not exist: {audio}",
        )
    try:
        payload = _ffprobe_json(audio)
    except _ProbeError as exc:
        return _envelope(
            "qa_audio_completeness",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            error=str(exc),
        )
    duration_s = _resolve_duration_s(payload)
    if duration_s is None or duration_s < MIN_NARRATION_DURATION_S:
        return _envelope(
            "qa_audio_completeness",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            audio_duration_s=duration_s,
            error=(
                f"audio too short or unparseable (duration_s={duration_s}, "
                f"floor={MIN_NARRATION_DURATION_S})"
            ),
        )
    try:
        silence_segments = _detect_silence_segments(
            audio,
            noise_db=float(silence_noise_db),
            min_duration_s=0.05,
        )
    except _ProbeError as exc:
        return _envelope(
            "qa_audio_completeness",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            audio_duration_s=duration_s,
            error=str(exc),
        )
    trailing_silence_s = 0.0
    if silence_segments:
        last_start, last_end = silence_segments[-1]
        effective_end = duration_s if last_end != last_end else last_end
        if effective_end >= duration_s - 1e-3:
            trailing_silence_s = max(0.0, effective_end - last_start)
    try:
        tail_rms_db = _measure_tail_rms_db(
            audio,
            duration_s=duration_s,
            tail_window_s=float(tail_window_s),
        )
    except _ProbeError as exc:
        return _envelope(
            "qa_audio_completeness",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            audio_duration_s=duration_s,
            trailing_silence_s=trailing_silence_s,
            error=str(exc),
        )
    silence_pass = trailing_silence_s >= float(min_trailing_silence_s)
    rms_pass = tail_rms_db == float("-inf") or tail_rms_db <= float(max_tail_rms_db)
    verdict = VERDICT_PASS if (silence_pass or rms_pass) else VERDICT_FAIL
    out = _envelope(
        "qa_audio_completeness",
        scene_id=scene_id,
        verdict=verdict,
        audio_path=str(audio),
        audio_duration_s=duration_s,
        trailing_silence_s=trailing_silence_s,
        tail_rms_db=tail_rms_db if tail_rms_db != float("-inf") else None,
    )
    if verdict == VERDICT_FAIL:
        reasons: list[str] = []
        if not silence_pass:
            reasons.append(
                f"trailing silence {trailing_silence_s:.3f} s < "
                f"{float(min_trailing_silence_s):.3f} s"
            )
        if not rms_pass:
            reasons.append(
                f"end-of-file RMS {tail_rms_db:.2f} dBFS > "
                f"{float(max_tail_rms_db):.2f} dBFS"
            )
        out["reason"] = "; ".join(reasons)
    return out


__all__ = [
    "qa_audio_completeness",
    "qa_duration_align",
    "qa_stills_judge",
    "qa_video_artifact_probe",
]
