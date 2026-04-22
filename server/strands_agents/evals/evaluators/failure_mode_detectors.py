"""Deterministic pixel-level failure-mode detectors for video fixtures.

These detectors operate on raw pixel data decoded from an mp4 file with
``ffmpeg``/``ffprobe`` — no LLM calls, no network, no heuristics based
on filename. Signals are intentionally coarse (mean brightness,
inter-frame absolute difference, container duration) so the thresholds
below stay stable across ffmpeg builds.

The detectors are used by :class:`FailureModeEvaluator` to grade
fixtures declared with ``expected_verdict == "reject"`` in
``fixtures/manifest.json`` — the failure-mode half of the corpus that
no judge is asked to adjudicate. Each detector returns a float in a
well-defined range so the evaluator can compare against a threshold.

Design notes
------------
* Frames are decoded once and reused across detectors to keep the
  fixture-suite runtime negligible (20 KB clip @ 15 fps @ 320x240 is
  about 4.6 MB decoded).
* Decoding is done via ``ffmpeg -f rawvideo -pix_fmt rgb24 -`` and
  numpy reshape — no opencv dependency. The fixtures are all short
  so the whole clip fits comfortably in memory.
* Duration is read via ``ffprobe`` so we trust the container header
  rather than ``frames / fps`` (which would hide truncation bugs).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

_FFMPEG_BIN = "ffmpeg"
_FFPROBE_BIN = "ffprobe"


@dataclass(frozen=True)
class VideoProbe:
    """Container-level metadata extracted by ``ffprobe``.

    Attributes:
        width: Frame width in pixels.
        height: Frame height in pixels.
        fps: Declared frames-per-second, as a float.
        duration_sec: Container duration in seconds.
        nb_frames: Declared frame count. May be ``None`` when the
            container did not record one (some older muxers).
    """

    width: int
    height: int
    fps: float
    duration_sec: float
    nb_frames: int | None


def probe_video(path: Path) -> VideoProbe:
    """Read metadata from a video file with ``ffprobe``.

    Args:
        path: Absolute path to the mp4 (or any ffprobe-readable file).

    Returns:
        :class:`VideoProbe` populated from the first video stream.

    Raises:
        RuntimeError: If ``ffprobe`` is not on PATH, returns non-zero,
            or the first stream is missing fields we require.
    """
    if shutil.which(_FFPROBE_BIN) is None:
        raise RuntimeError("ffprobe binary not found on PATH")

    result = subprocess.run(  # noqa: S603 — args are a fixed literal list
        [
            _FFPROBE_BIN,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate,nb_frames:format=duration",
            "-of", "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed on {path} (code {result.returncode}):\n"
            f"{result.stderr}"
        )

    data: dict[str, Any] = json.loads(result.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"ffprobe reported no video streams in {path}")
    stream = streams[0]
    fmt = data.get("format") or {}

    width = int(stream["width"])
    height = int(stream["height"])
    fps = _parse_rational(stream.get("r_frame_rate", "0/1"))
    duration_sec = float(fmt.get("duration") or stream.get("duration") or 0.0)
    nb_frames_raw = stream.get("nb_frames")
    nb_frames: int | None
    try:
        nb_frames = int(nb_frames_raw) if nb_frames_raw is not None else None
    except (TypeError, ValueError):
        nb_frames = None

    return VideoProbe(
        width=width,
        height=height,
        fps=fps,
        duration_sec=duration_sec,
        nb_frames=nb_frames,
    )


def _parse_rational(raw: str) -> float:
    """Parse an ffprobe rational like ``"15/1"`` into a float."""
    if "/" in raw:
        num, _, den = raw.partition("/")
        try:
            denom = float(den) or 1.0
            return float(num) / denom
        except ValueError:
            return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def decode_frames(path: Path, probe: VideoProbe | None = None) -> np.ndarray:
    """Decode the entire video into an ``(N, H, W, 3)`` uint8 array.

    Args:
        path: Absolute path to an mp4 (or any ffmpeg-readable file).
        probe: Optional pre-computed :class:`VideoProbe`. Passed in
            when the caller already probed the file to avoid a second
            ``ffprobe`` spawn.

    Returns:
        RGB24 pixel array with shape ``(frames, height, width, 3)``
        and dtype ``uint8``. Byte ordering is R, G, B.

    Raises:
        RuntimeError: If ``ffmpeg`` is missing, fails, or produces an
            output size that is not a whole multiple of one frame.
    """
    if shutil.which(_FFMPEG_BIN) is None:
        raise RuntimeError("ffmpeg binary not found on PATH")

    if probe is None:
        probe = probe_video(path)

    result = subprocess.run(  # noqa: S603 — args are a fixed literal list
        [
            _FFMPEG_BIN,
            "-v", "error",
            "-i", str(path),
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed to decode {path} (code {result.returncode}):\n"
            f"{result.stderr.decode('utf-8', errors='replace')}"
        )

    frame_bytes = probe.width * probe.height * 3
    if frame_bytes == 0:
        raise RuntimeError(f"zero-sized frame for {path}: {probe}")
    total = len(result.stdout)
    if total % frame_bytes != 0:
        raise RuntimeError(
            f"ffmpeg produced {total} bytes for {path} "
            f"which is not a whole number of frames "
            f"({probe.width}x{probe.height}x3 = {frame_bytes})"
        )

    frames = np.frombuffer(result.stdout, dtype=np.uint8).reshape(
        (-1, probe.height, probe.width, 3)
    )
    return frames


def black_frame_ratio(frames: np.ndarray, threshold: int = 20) -> float:
    """Fraction of frames whose mean brightness is below ``threshold``.

    Each frame's brightness is the mean of its RGB values on a 0-255
    scale. A pure-black fixture encoded at constant-QP will sit a few
    values above zero because of libx264's colour-space rounding; a
    threshold of 20 covers that noise floor without admitting dark-
    scene content.

    Args:
        frames: ``(N, H, W, 3)`` uint8 array.
        threshold: Inclusive upper bound on mean brightness that
            still counts as a "black" frame.

    Returns:
        Ratio in ``[0.0, 1.0]``. ``0.0`` for a bright fixture, ``1.0``
        when every frame looks black.
    """
    if frames.size == 0:
        return 0.0
    per_frame_mean = frames.astype(np.float32).mean(axis=(1, 2, 3))
    return float((per_frame_mean <= threshold).mean())


def white_frame_ratio(frames: np.ndarray, threshold: int = 235) -> float:
    """Fraction of frames whose mean brightness is above ``threshold``.

    Mirrors :func:`black_frame_ratio` for blown-out/white-out clips.
    Threshold chosen to admit the residual chroma wobble of a solid-
    white encode while still rejecting any fixture with meaningful
    content.

    Args:
        frames: ``(N, H, W, 3)`` uint8 array.
        threshold: Inclusive lower bound on mean brightness that
            counts as a "white" frame.

    Returns:
        Ratio in ``[0.0, 1.0]``.
    """
    if frames.size == 0:
        return 0.0
    per_frame_mean = frames.astype(np.float32).mean(axis=(1, 2, 3))
    return float((per_frame_mean >= threshold).mean())


def max_interframe_diff(frames: np.ndarray) -> float:
    """Maximum mean absolute difference between consecutive frames.

    A frozen-frame fixture encodes as a loop of a single source frame;
    libx264 yuv420p rounding introduces tiny per-frame noise but the
    mean absolute difference stays well under a single 0-255 value.
    A clip with any visible motion easily produces several units of
    mean difference. Returning the ``max`` over all consecutive pairs
    rather than the mean keeps a single-frame glitch from being
    washed out by a long still segment.

    Args:
        frames: ``(N, H, W, 3)`` uint8 array. ``N < 2`` returns
            ``0.0`` (no pairs to compare).

    Returns:
        Max mean absolute difference across all ``(i, i+1)`` pairs,
        in the same 0-255 scale as the pixels. ``0.0`` means
        every frame is byte-identical.
    """
    if frames.shape[0] < 2:
        return 0.0
    diffs = np.abs(
        frames[1:].astype(np.int16) - frames[:-1].astype(np.int16)
    ).astype(np.float32)
    per_pair_mean = diffs.mean(axis=(1, 2, 3))
    return float(per_pair_mean.max())


@dataclass(frozen=True)
class VideoSignals:
    """Bundle of all signals extracted from one video fixture."""

    width: int
    height: int
    fps: float
    duration_sec: float
    frame_count: int
    black_ratio: float
    white_ratio: float
    max_interframe_diff: float


def extract_signals(path: Path) -> VideoSignals:
    """Run the full detector stack against one file.

    Convenience wrapper: probes the container, decodes the frames,
    and runs every detector. Returns a :class:`VideoSignals` that
    the :class:`FailureModeEvaluator` grades against per-case
    thresholds.

    Args:
        path: Absolute path to an mp4 (or any ffmpeg-readable file).

    Returns:
        :class:`VideoSignals` with every detector's score populated.
    """
    probe = probe_video(path)
    frames = decode_frames(path, probe=probe)
    return VideoSignals(
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        duration_sec=probe.duration_sec,
        frame_count=int(frames.shape[0]),
        black_ratio=black_frame_ratio(frames),
        white_ratio=white_frame_ratio(frames),
        max_interframe_diff=max_interframe_diff(frames),
    )


__all__ = [
    "VideoProbe",
    "VideoSignals",
    "black_frame_ratio",
    "decode_frames",
    "extract_signals",
    "max_interframe_diff",
    "probe_video",
    "white_frame_ratio",
]
