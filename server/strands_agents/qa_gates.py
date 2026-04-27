"""Slice 9l — pixel- and ffprobe-level quality gates wired as orchestrator tools.

The slice 9j-quality-pass run shipped a frozen-frame "documentary"
because every gate in the live path was *plumbing* (bytes flowed,
ffmpeg ran, file had ``ftypisom`` magic). None of them decoded a
frame or compared per-scene narration duration to per-scene rendered
video duration. The mode duality (deleted in slice 9k / PR #371) hid
this further: the scripted demo brain auto-approved every gate
without invoking the LLM-backed evaluators that exist in
``/components``.

This module ships three deterministic, credential-free gates the
orchestrator must call on every scene before assembly:

* :func:`qa_duration_align` — ffprobes the audio and video artifacts
  and hard-fails when ``|audio_dur - video_dur| > tolerance``. Catches
  the exact slice 9j regression: a 3.7 s video paired with a 13 s
  narration trips the gate at ``delta=9.3 s``.
* :func:`qa_stills_judge` — decodes ``num_samples`` evenly-spaced
  frames with ffmpeg and computes mean inter-frame L1 pixel delta.
  Hard-fails when the mean delta drops below ``min_mean_pixel_delta``.
  This catches a video that has the right duration but is visually
  frozen (the muxer pads the last frame, or LTX-2.3 emits N
  near-identical frames). Pure numpy — no vision LLM required, so
  CI stays hermetic.
* :func:`qa_video_artifact_probe` — thin wrapper around ffprobe
  exposing duration / frame count / dimensions / codec. The other
  two gates use it internally; exposing it as a tool gives the
  orchestrator (and the escalation SubAgent) a way to inspect an
  artifact without a second ffprobe invocation.

All three return the same envelope shape::

    {
        "tool": "<gate name>",
        "verdict": "pass" | "fail",
        "scene_id": "scene_<n>",
        ...gate-specific evidence fields...,
    }

When ``verdict == "fail"`` the orchestrator must delegate to the
``escalation`` SubAgent per AGENTS.md hard invariants §3-5 ("fail
closed on TTS / video render", "QA immediately after each artifact").
The orchestrator prompt enforces this; see ``ORCHESTRATOR_PROMPT``
in :mod:`strands_agents.pipeline`.

The gates are intentionally **deterministic** so they survive a
hermetic CI run: same MP4 in, same verdict out. The vision-LLM
visual-coherence judge that lives in
:mod:`strands_agents.coherence_evaluator` operates on concept-level
text (style_lock + shot_type strings) and is invoked at a
different point in the pipeline.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


#: Default tolerance for :func:`qa_duration_align`. Anything tighter
#: false-positives on legitimate worker variance (LTX-2.3 rounds
#: frame counts to the 8k+1 grid; Qwen3-TTS adds a small trailing
#: silence). 0.5 s is what the user instructed in the slice 9j
#: post-mortem ("hard fail if |delta| > 0.5s").
DEFAULT_DURATION_TOLERANCE_S: float = 0.5


#: Default frame-sample count for :func:`qa_stills_judge`. Eight
#: evenly-spaced samples covers a 4-15 s clip well — every sample
#: lands in a different ~0.5 s window so the mean delta is dominated
#: by motion, not by adjacent-frame noise.
DEFAULT_STILLS_NUM_SAMPLES: int = 8


#: Minimum mean inter-frame L1 pixel delta (0-255 scale). Below this
#: the clip is judged a still. Determined empirically from LTX-2.3
#: BASIC outputs: a healthy clip averages ~12-25 (slow camera
#: documentary shots), a fully frozen clip averages ~0.05 (only
#: encoding noise). 1.5 is comfortably above noise floor and below
#: any real-motion clip we've sampled.
DEFAULT_MIN_MEAN_PIXEL_DELTA: float = 1.5


#: Minimum video size (bytes) we will probe. Smaller files are
#: certainly truncated and we'd rather fail loudly than have
#: ffprobe report ``duration_sec=0`` and confuse the orchestrator.
MIN_VIDEO_BYTES: int = 1024


VERDICT_PASS: str = "pass"
VERDICT_FAIL: str = "fail"


class _ProbeError(RuntimeError):
    """Raised when ffprobe fails or returns an unparseable payload."""


def _ffprobe_path() -> str:
    """Resolve ffprobe on ``PATH``; raise loudly when missing."""
    found = shutil.which("ffprobe")
    if not found:
        raise _ProbeError("ffprobe not found on PATH")
    return found


def _ffmpeg_path() -> str:
    """Resolve ffmpeg on ``PATH``; raise loudly when missing."""
    found = shutil.which("ffmpeg")
    if not found:
        raise _ProbeError("ffmpeg not found on PATH")
    return found


def _ffprobe_json(media_path: Path) -> dict[str, Any]:
    """Run ``ffprobe -of json`` and return the parsed payload."""
    cmd = [
        _ffprobe_path(),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(media_path),
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except subprocess.CalledProcessError as exc:
        raise _ProbeError(
            f"ffprobe failed for {media_path}: rc={exc.returncode} "
            f"stderr={exc.stderr.strip()[:200]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _ProbeError(f"ffprobe timed out for {media_path}") from exc
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise _ProbeError(
            f"ffprobe emitted non-JSON for {media_path}: {completed.stdout[:200]}"
        ) from exc


def _resolve_duration_s(payload: dict[str, Any]) -> float | None:
    """Extract a duration in seconds from an ffprobe payload.

    Tries ``format.duration`` first (most reliable for muxed files),
    then the first stream's ``duration``. Returns ``None`` when both
    are missing or unparseable so the caller can fail loudly with
    context instead of silently treating it as zero.
    """
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
    """Parse a binary PGM (P5) header and return ``(w, h, pixels)``.

    ffmpeg writes PGM with format ``P5\\n<w> <h>\\n<maxval>\\n<bytes>``.
    Used by :func:`qa_stills_judge` to decode greyscale frames
    without pulling in PIL or numpy as a hard dependency.
    """
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
    """Decode ``num_samples`` evenly-spaced greyscale frames as raw bytes.

    Uses ffmpeg's ``select`` filter to pluck frames at fixed timestamps
    (avoids the cost of decoding every frame), then ``scale`` +
    ``format=gray`` to compress them to a small fixed-size greyscale
    buffer for cheap pixel-delta math. Reads PGM straight off
    stdout so we don't touch the filesystem twice.
    """
    payload = _ffprobe_json(video_path)
    duration = _resolve_duration_s(payload)
    if duration is None or duration <= 0:
        raise _ProbeError(f"video {video_path} has no usable duration")
    # Sample at the *centre* of each evenly-spaced slot; avoids hitting
    # the very first / last frame where intro fades artificially
    # depress the inter-frame delta.
    samples: list[bytes] = []
    for i in range(num_samples):
        t = duration * (i + 0.5) / num_samples
        cmd = [
            _ffmpeg_path(),
            "-loglevel",
            "error",
            "-ss",
            f"{t:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:{height},format=gray",
            "-c:v",
            "pgm",
            "-f",
            "image2pipe",
            "-",
        ]
        try:
            completed = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                timeout=15.0,
            )
        except subprocess.CalledProcessError as exc:
            raise _ProbeError(
                f"ffmpeg sample at t={t:.3f}s failed for {video_path}: "
                f"stderr={exc.stderr.decode('utf-8', 'replace')[:200]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise _ProbeError(
                f"ffmpeg sample at t={t:.3f}s timed out for {video_path}"
            ) from exc
        _, _, pixels = _decode_pgm(completed.stdout)
        samples.append(pixels)
    return samples


def _mean_l1_delta(frames: list[bytes]) -> float:
    """Mean per-pixel L1 delta between consecutive frame pairs."""
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


@tool
def qa_video_artifact_probe(
    scene_id: str,
    video_path: str,
) -> dict[str, Any]:
    """Probe a rendered MP4 with ffprobe and return its key facts.

    Returns ``duration_s``, ``width``, ``height``, ``codec``,
    ``nb_frames`` (when the codec exposes it), and the on-disk size
    in bytes. Verdict is ``pass`` when probe succeeds, ``fail`` on
    any error (file missing, ffprobe failure, no video stream).
    The orchestrator can call this directly or rely on the gates
    that wrap it.
    """
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


@tool
def qa_duration_align(
    scene_id: str,
    audio_path: str,
    video_path: str,
    tolerance_s: float = DEFAULT_DURATION_TOLERANCE_S,
) -> dict[str, Any]:
    """Hard-fail when audio and video durations diverge.

    AGENTS.md hard invariant §5 ("QA immediately after each
    artifact") requires this gate to run after every
    ``launch_visual_production`` whose paired ``launch_audio_render``
    has already returned. The orchestrator must escalate via the
    ``escalation`` SubAgent on ``verdict == "fail"``.

    Slice 9j-quality-pass shipped frozen frames because no such
    gate existed: a 3.7 s video paired with a 13 s narration tripped
    nothing. With a 0.5 s tolerance this gate would have fired
    ``delta=9.3 s`` and prevented the run from reaching assembly.

    Args:
        scene_id: The scene this artifact belongs to.
        audio_path: Filesystem path to the rendered audio
            (WAV, MP3, etc.; ffprobe figures out the container).
        video_path: Filesystem path to the rendered MP4.
        tolerance_s: Max ``|audio_dur - video_dur|`` before failing.
            Defaults to 0.5 s, the ceiling the operator set in the
            slice 9j post-mortem.
    """
    audio = Path(audio_path)
    video = Path(video_path)
    if not audio.is_file():
        return _envelope(
            "qa_duration_align",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            video_path=str(video),
            tolerance_s=float(tolerance_s),
            error=f"audio_path does not exist: {audio}",
        )
    if not video.is_file():
        return _envelope(
            "qa_duration_align",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            audio_path=str(audio),
            video_path=str(video),
            tolerance_s=float(tolerance_s),
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
            tolerance_s=float(tolerance_s),
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
            tolerance_s=float(tolerance_s),
            error="ffprobe returned no duration on at least one artifact",
        )
    delta_s = abs(audio_dur - video_dur)
    verdict = VERDICT_PASS if delta_s <= float(tolerance_s) else VERDICT_FAIL
    out = _envelope(
        "qa_duration_align",
        scene_id=scene_id,
        verdict=verdict,
        audio_path=str(audio),
        video_path=str(video),
        audio_duration_s=audio_dur,
        video_duration_s=video_dur,
        delta_s=delta_s,
        tolerance_s=float(tolerance_s),
    )
    if verdict == VERDICT_FAIL:
        out["reason"] = (
            f"|audio_dur - video_dur| = {delta_s:.3f} s > "
            f"tolerance {float(tolerance_s):.3f} s"
        )
    return out


@tool
def qa_stills_judge(
    scene_id: str,
    video_path: str,
    num_samples: int = DEFAULT_STILLS_NUM_SAMPLES,
    min_mean_pixel_delta: float = DEFAULT_MIN_MEAN_PIXEL_DELTA,
) -> dict[str, Any]:
    """Hard-fail when a rendered video has no meaningful motion.

    Decodes ``num_samples`` evenly-spaced greyscale frames and
    computes the mean L1 pixel delta between consecutive samples.
    Below ``min_mean_pixel_delta`` (default 1.5 on a 0-255 scale)
    the clip is judged a still — either LTX-2.3 produced
    near-identical frames, or the muxer froze the last frame and
    padded to longer duration.

    The user explicitly flagged stills as a hard-fail in the slice
    9j post-mortem ("stills are a hard fail for an LLM-based
    check"). This deterministic implementation is the
    credential-free floor; a vision-LLM judge can layer over the
    same envelope without changing the orchestrator contract.

    Args:
        scene_id: The scene this artifact belongs to.
        video_path: Filesystem path to the rendered MP4.
        num_samples: Number of evenly-spaced frames to sample.
            Defaults to 8.
        min_mean_pixel_delta: Floor for the mean inter-frame L1
            delta. Below this the verdict is ``fail``. Defaults
            to 1.5.
    """
    if num_samples < 2:
        raise ValueError("num_samples must be >= 2")
    video = Path(video_path)
    if not video.is_file():
        return _envelope(
            "qa_stills_judge",
            scene_id=scene_id,
            verdict=VERDICT_FAIL,
            video_path=str(video),
            num_samples=int(num_samples),
            min_mean_pixel_delta=float(min_mean_pixel_delta),
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
            num_samples=int(num_samples),
            min_mean_pixel_delta=float(min_mean_pixel_delta),
            error=str(exc),
        )
    mean_delta = _mean_l1_delta(frames)
    verdict = (
        VERDICT_PASS
        if mean_delta >= float(min_mean_pixel_delta)
        else VERDICT_FAIL
    )
    out = _envelope(
        "qa_stills_judge",
        scene_id=scene_id,
        verdict=verdict,
        video_path=str(video),
        num_samples=int(num_samples),
        min_mean_pixel_delta=float(min_mean_pixel_delta),
        mean_pixel_delta=mean_delta,
    )
    if verdict == VERDICT_FAIL:
        out["reason"] = (
            f"mean inter-frame pixel delta {mean_delta:.3f} below "
            f"floor {float(min_mean_pixel_delta):.3f}"
        )
    return out


__all__ = [
    "DEFAULT_DURATION_TOLERANCE_S",
    "DEFAULT_MIN_MEAN_PIXEL_DELTA",
    "DEFAULT_STILLS_NUM_SAMPLES",
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "qa_duration_align",
    "qa_stills_judge",
    "qa_video_artifact_probe",
]
