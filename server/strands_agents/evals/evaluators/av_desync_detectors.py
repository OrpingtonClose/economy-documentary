"""Pure signal-level detectors for combined audio+video fixtures.

Offline, deterministic, no LLM calls, no external service beyond the
``ffmpeg`` / ``ffprobe`` binaries used to decode mp4 streams into raw
samples and frames. All analysis runs on

* mono 16-bit PCM samples decoded via ``ffmpeg -vn -f wav`` and the
  stdlib :mod:`wave` module, and
* grayscale luminance frames decoded via ``ffmpeg -vf
  'scale=80:60,format=gray'`` and read as PGM bytes.

The detectors produce three kinds of signals:

* **Audio onset** — the wall-clock second at which the audio stream
  first exceeds an RMS threshold inside a short window. ``None`` if
  the whole clip stays below the threshold (silent track).
* **Video content onset** — the wall-clock second of the first
  frame whose mean luminance exceeds a threshold. ``None`` if the
  whole clip stays near black.
* **A/V desync** — ``audio_onset - video_content_onset``, or a
  sentinel when either signal is missing.

These primitives are consumed by
:class:`strands_agents.evals.evaluators.av_desync.AVDesyncEvaluator`
to gate the assembly-layer invariant "audio and video onsets fall
within ±150 ms and both rails carry signal".

Determinism notes
-----------------
ffprobe/ffmpeg are invoked with explicit decode parameters (pin
``-ar 16000``, ``-ac 1``, ``scale=80:60,format=gray``) so hashable
outputs are stable across hosts that ship the same ffmpeg major
version. Windowed RMS and mean-luminance computations are pure
numpy and do not depend on ffmpeg's internal state past decode.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_FFMPEG_BIN = "ffmpeg"
_FFPROBE_BIN = "ffprobe"

# Window over which audio RMS is computed when scanning for onset.
# 20 ms at 16 kHz is 320 samples — a short-enough window that even a
# single syllable registers, long enough that a stray sample can't
# trigger onset.
DEFAULT_AUDIO_ONSET_WIN_MS: float = 20.0

# Threshold on the 20 ms windowed RMS, in the ``[0.0, 1.0]`` sample
# range. A clean narration clip crosses this within 50 ms of speech
# start; a silent track never does.
DEFAULT_AUDIO_ONSET_RMS_THRESHOLD: float = 0.02

# Mean luminance of a decoded grayscale frame (0-255) below which
# the frame is treated as "black". 20 is a loose threshold that
# still classifies the low-energy x264 mosquito noise on a true
# black input as black but rejects any rendered content.
DEFAULT_VIDEO_CONTENT_LUM_THRESHOLD: float = 20.0

# Sampling rate for the video-content scan. We sample every mp4
# frame (at the video's native fps) up to this cap — past 15 fps
# the detector is already saturated by an ffmpeg decode per frame.
DEFAULT_VIDEO_CONTENT_MAX_FPS: float = 15.0

# Downscale target for the grayscale decode. 80×60 = 4800 pixels,
# small enough to decode hundreds of frames per second, large
# enough that a single lit word registers above the luminance
# threshold.
_LUMA_DECODE_W: int = 80
_LUMA_DECODE_H: int = 60

# Output sample rate / channel-count for the audio extract. These
# match what the audio-failure-mode detectors expect.
_AUDIO_DECODE_SR: int = 16000
_AUDIO_DECODE_CHANNELS: int = 1


@dataclass(frozen=True)
class AVProbe:
    """Stream-topology metadata for an mp4 fixture.

    Attributes:
        duration_sec: Format-reported duration (seconds).
        has_video_stream: Whether the file contains at least one
            video stream.
        has_audio_stream: Whether the file contains at least one
            audio stream.
        video_duration_sec: Duration of stream 0 of type video, or
            ``0.0`` if no video stream is present.
        audio_duration_sec: Duration of stream 0 of type audio, or
            ``0.0`` if no audio stream is present.
        video_fps: Frame rate of the video stream as a decimal, or
            ``0.0`` if no video stream is present.
    """

    duration_sec: float
    has_video_stream: bool
    has_audio_stream: bool
    video_duration_sec: float
    audio_duration_sec: float
    video_fps: float


@dataclass(frozen=True)
class AVSignals:
    """Envelope returned by :func:`extract_av_signals`.

    Attributes:
        path: Source mp4 path (string form).
        duration_sec: Overall format duration from the probe.
        has_video_stream: Mirrors :class:`AVProbe`.
        has_audio_stream: Mirrors :class:`AVProbe`.
        audio_onset_sec: First second at which the audio RMS
            crosses :data:`DEFAULT_AUDIO_ONSET_RMS_THRESHOLD`, or
            ``None`` if no crossing is found.
        video_content_onset_sec: First second at which a decoded
            grayscale frame exceeds
            :data:`DEFAULT_VIDEO_CONTENT_LUM_THRESHOLD`, or ``None``
            if no such frame exists.
        desync_sec: ``audio_onset_sec - video_content_onset_sec``.
            ``None`` if either onset is missing.
        audio_rms: RMS amplitude of the full audio stream
            (``0.0`` if no audio stream).
    """

    path: str
    duration_sec: float
    has_video_stream: bool
    has_audio_stream: bool
    audio_onset_sec: float | None
    video_content_onset_sec: float | None
    desync_sec: float | None
    audio_rms: float


def _require_binaries() -> None:
    """Raise :class:`RuntimeError` if ffmpeg/ffprobe are missing."""
    if shutil.which(_FFMPEG_BIN) is None:
        raise RuntimeError(
            "ffmpeg binary not found on PATH — install ffmpeg to run "
            "AV desync detectors"
        )
    if shutil.which(_FFPROBE_BIN) is None:
        raise RuntimeError(
            "ffprobe binary not found on PATH — install ffmpeg to run "
            "AV desync detectors"
        )


def probe_av(path: Path) -> AVProbe:
    """Probe an mp4 for its stream topology and durations.

    Args:
        path: Path to an mp4 file.

    Returns:
        :class:`AVProbe` describing which streams are present and
        their individual durations.

    Raises:
        RuntimeError: If ffprobe is not available on ``PATH``.
        FileNotFoundError: If ``path`` does not exist.
    """
    _require_binaries()
    if not path.exists():
        raise FileNotFoundError(f"mp4 fixture not found: {path}")

    fmt_out = subprocess.run(
        [
            _FFPROBE_BIN,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    duration_sec = float(fmt_out) if fmt_out else 0.0

    streams_out = subprocess.run(
        [
            _FFPROBE_BIN,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,duration,r_frame_rate",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    has_video = False
    has_audio = False
    video_duration = 0.0
    audio_duration = 0.0
    video_fps = 0.0
    for line in streams_out.splitlines():
        parts = line.split(",")
        if not parts:
            continue
        codec_type = parts[0].strip()
        if codec_type == "video":
            has_video = True
            if len(parts) >= 2 and parts[1]:
                try:
                    video_fps = _parse_rational(parts[1])
                except ValueError:
                    video_fps = 0.0
            if len(parts) >= 3 and parts[2]:
                try:
                    video_duration = float(parts[2])
                except ValueError:
                    video_duration = 0.0
        elif codec_type == "audio":
            has_audio = True
            # stream=codec_type,duration,r_frame_rate → for audio,
            # r_frame_rate is usually 0/0 and duration is parts[2].
            if len(parts) >= 3 and parts[2]:
                try:
                    audio_duration = float(parts[2])
                except ValueError:
                    audio_duration = 0.0

    return AVProbe(
        duration_sec=duration_sec,
        has_video_stream=has_video,
        has_audio_stream=has_audio,
        video_duration_sec=video_duration,
        audio_duration_sec=audio_duration,
        video_fps=video_fps,
    )


def _parse_rational(s: str) -> float:
    """Parse ``"30000/1001"`` or ``"15/1"`` into a float. Raises on ``0/0``."""
    if "/" in s:
        num, den = s.split("/", 1)
        num_f = float(num)
        den_f = float(den)
        if den_f == 0:
            raise ValueError(f"zero denominator in rational {s!r}")
        return num_f / den_f
    return float(s)


def extract_audio_samples(path: Path) -> tuple[np.ndarray, int]:
    """Decode the audio stream of ``path`` into mono int16 → float32.

    Args:
        path: Path to an mp4 file. Must contain an audio stream.

    Returns:
        Tuple ``(samples, sample_rate)`` where ``samples`` is a 1-D
        float32 array in the ``[-1.0, 1.0]`` range. If the mp4 has
        no audio stream, returns an empty array and the default
        decode sample rate.

    Raises:
        RuntimeError: If ffmpeg is not available on ``PATH``.
        FileNotFoundError: If ``path`` does not exist.
    """
    _require_binaries()
    if not path.exists():
        raise FileNotFoundError(f"mp4 fixture not found: {path}")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    try:
        result = subprocess.run(
            [
                _FFMPEG_BIN,
                "-y",
                "-i",
                str(path),
                "-vn",
                "-ac",
                str(_AUDIO_DECODE_CHANNELS),
                "-ar",
                str(_AUDIO_DECODE_SR),
                "-f",
                "wav",
                wav_path,
                "-loglevel",
                "error",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # No audio stream is a valid condition — callers will
            # pair this with probe_av to know the stream was
            # missing. Return an empty envelope.
            return np.zeros(0, dtype=np.float32), _AUDIO_DECODE_SR

        with wave.open(wav_path, "rb") as w:
            sr = w.getframerate()
            num_samples = w.getnframes()
            frames = w.readframes(num_samples)
    finally:
        try:
            os.unlink(wav_path)
        except FileNotFoundError:
            pass

    if not frames:
        return np.zeros(0, dtype=np.float32), sr

    raw = np.frombuffer(frames, dtype=np.int16)
    return raw.astype(np.float32) / 32768.0, sr


def audio_onset_sec(
    samples: np.ndarray,
    sample_rate: int,
    *,
    threshold: float = DEFAULT_AUDIO_ONSET_RMS_THRESHOLD,
    win_ms: float = DEFAULT_AUDIO_ONSET_WIN_MS,
) -> float | None:
    """Find the first second at which windowed RMS crosses ``threshold``.

    Args:
        samples: 1-D float32 samples in ``[-1.0, 1.0]``.
        sample_rate: Sample rate of ``samples``.
        threshold: Minimum windowed RMS that counts as "audio
            present". Defaults to
            :data:`DEFAULT_AUDIO_ONSET_RMS_THRESHOLD`.
        win_ms: Window length in milliseconds. Defaults to
            :data:`DEFAULT_AUDIO_ONSET_WIN_MS`.

    Returns:
        Onset timestamp in seconds, or ``None`` if no window
        crosses the threshold.
    """
    if sample_rate <= 0 or samples.size == 0:
        return None
    win = max(1, int(sample_rate * win_ms / 1000.0))
    if samples.size < win:
        return None
    # Hop by the full window so the cost stays O(n / win).
    for i in range(0, samples.size - win + 1, win):
        chunk = samples[i : i + win]
        rms = float(np.sqrt(np.mean(chunk * chunk)))
        if rms >= threshold:
            return i / sample_rate
    return None


def _decode_frame_luma(path: Path, timestamp_sec: float) -> float:
    """Decode one grayscale frame at ``timestamp_sec`` and return mean luminance.

    Args:
        path: Path to an mp4 file.
        timestamp_sec: Seek timestamp in seconds.

    Returns:
        Mean 0-255 luminance of the decoded frame. Returns ``0.0``
        when ffmpeg fails to decode a frame at the requested
        timestamp (e.g. past end-of-stream).
    """
    with tempfile.NamedTemporaryFile(suffix=".pgm", delete=False) as f:
        pgm_path = f.name
    try:
        result = subprocess.run(
            [
                _FFMPEG_BIN,
                "-y",
                "-ss",
                f"{timestamp_sec:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                f"scale={_LUMA_DECODE_W}:{_LUMA_DECODE_H},format=gray",
                "-f",
                "image2",
                pgm_path,
                "-loglevel",
                "error",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return 0.0

        with open(pgm_path, "rb") as fh:
            header = b""
            # A PGM header is three ascii lines: "P5\n", "W H\n",
            # "MAXVAL\n". Read byte-by-byte until we've seen three
            # newlines, then the rest is raw pixel bytes.
            while header.count(b"\n") < 3:
                byte = fh.read(1)
                if not byte:
                    return 0.0
                header += byte
            raw = fh.read()
    finally:
        try:
            os.unlink(pgm_path)
        except FileNotFoundError:
            pass

    if not raw:
        return 0.0
    return float(np.frombuffer(raw, dtype=np.uint8).astype(np.float32).mean())


def video_content_onset_sec(
    path: Path,
    probe: AVProbe,
    *,
    luminance_threshold: float = DEFAULT_VIDEO_CONTENT_LUM_THRESHOLD,
    max_fps: float = DEFAULT_VIDEO_CONTENT_MAX_FPS,
) -> float | None:
    """Find the first second at which mean frame luminance crosses ``luminance_threshold``.

    Frames are sampled at ``min(video_fps, max_fps)`` from t=0 until
    the end of the video stream, one ffmpeg decode per frame. The
    sampled values are the decoded frame's mean 0-255 luminance.

    Args:
        path: Path to the mp4 file being probed.
        probe: :class:`AVProbe` already computed for ``path``.
        luminance_threshold: Mean-luminance cutoff (0-255) above
            which a frame counts as "visible content". Defaults to
            :data:`DEFAULT_VIDEO_CONTENT_LUM_THRESHOLD`.
        max_fps: Upper bound on sampling rate. Defaults to
            :data:`DEFAULT_VIDEO_CONTENT_MAX_FPS`.

    Returns:
        Onset timestamp in seconds, or ``None`` if no frame crosses
        the threshold (pure black clip) or if the mp4 has no video
        stream.
    """
    if not probe.has_video_stream or probe.video_duration_sec <= 0:
        return None
    sample_fps = probe.video_fps if probe.video_fps > 0 else max_fps
    sample_fps = min(sample_fps, max_fps)
    num_samples = max(1, int(probe.video_duration_sec * sample_fps))
    for i in range(num_samples):
        ts = i / sample_fps
        lum = _decode_frame_luma(path, ts)
        if lum >= luminance_threshold:
            return ts
    return None


def extract_av_signals(path: Path) -> AVSignals:
    """Probe + decode + measure → one :class:`AVSignals` envelope.

    This is the single convenience entry point used by the
    experiment's ``task`` callable. Unit tests exercise the
    primitives (:func:`audio_onset_sec`,
    :func:`video_content_onset_sec`) directly on synthetic signals
    to stay fast and deterministic.

    Args:
        path: Path to an mp4 fixture.

    Returns:
        :class:`AVSignals` with both onsets, the computed
        ``desync_sec`` (``None`` if either onset is missing), and
        the overall audio RMS for reference.
    """
    probe = probe_av(path)
    samples, sample_rate = extract_audio_samples(path)
    if samples.size > 0:
        audio_onset = audio_onset_sec(samples, sample_rate)
        audio_rms = float(np.sqrt(np.mean(samples * samples)))
    else:
        audio_onset = None
        audio_rms = 0.0
    video_onset = video_content_onset_sec(path, probe)
    if audio_onset is None or video_onset is None:
        desync = None
    else:
        desync = audio_onset - video_onset
    return AVSignals(
        path=str(path),
        duration_sec=probe.duration_sec,
        has_video_stream=probe.has_video_stream,
        has_audio_stream=probe.has_audio_stream,
        audio_onset_sec=audio_onset,
        video_content_onset_sec=video_onset,
        desync_sec=desync,
        audio_rms=audio_rms,
    )


__all__ = [
    "DEFAULT_AUDIO_ONSET_RMS_THRESHOLD",
    "DEFAULT_AUDIO_ONSET_WIN_MS",
    "DEFAULT_VIDEO_CONTENT_LUM_THRESHOLD",
    "DEFAULT_VIDEO_CONTENT_MAX_FPS",
    "AVProbe",
    "AVSignals",
    "audio_onset_sec",
    "extract_audio_samples",
    "extract_av_signals",
    "probe_av",
    "video_content_onset_sec",
]
