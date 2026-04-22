"""Pure signal-level detectors for audio failure fixtures.

Offline, deterministic, no LLM calls, no external service. All analysis
runs on mono 16-bit PCM samples decoded via the stdlib :mod:`wave`
module and converted to float32 in the ``[-1.0, 1.0]`` range via
``numpy``.

Detectors
---------
* :func:`rms_level` — root-mean-square amplitude of the signal. Near
  ``0.0`` for silence, higher for real narration.
* :func:`peak_level` — maximum absolute sample amplitude. ``1.0``
  indicates clipping at the 16-bit PCM ceiling.
* :func:`clipping_ratio` — fraction of samples whose absolute value
  meets or exceeds a clip threshold (defaults to ``0.99``, i.e.
  samples that round to the int16 ceiling). A clean narration clip
  has a clipping ratio near ``0.0``.
* :func:`spectral_flatness` — geometric-mean / arithmetic-mean of
  the magnitude spectrum (Wiener entropy). ``1.0`` indicates a
  perfectly flat spectrum (white noise); ``0.0`` indicates a pure
  tone. Narration lives somewhere in between but is typically well
  below ``0.5``.
* :func:`extract_signals` — convenience wrapper that probes + loads
  + computes every primitive into one envelope.

The primitives are all pure numpy on a 1-D float32 array. They
operate on samples, not on file paths, so unit tests can inject
synthetic signals without touching the filesystem. The
:func:`extract_signals` convenience wrapper is the only function
that reads a WAV off disk.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Threshold considered "clipping" on the normalised ``[-1.0, 1.0]``
# sample range. 0.99 corresponds to samples within one int16 step of
# the ceiling, which is what "hit the rail" actually means once you
# account for quantisation — softer thresholds (0.95, 0.9) would
# false-positive on loud but legal narration.
DEFAULT_CLIP_THRESHOLD: float = 0.99


@dataclass(frozen=True)
class AudioProbe:
    """Result of :func:`probe_audio` — lightweight header metadata."""

    num_samples: int
    sample_rate: int
    num_channels: int
    sample_width_bytes: int
    duration_sec: float


@dataclass(frozen=True)
class AudioSignals:
    """Envelope returned by :func:`extract_signals`."""

    path: str
    duration_sec: float
    sample_rate: int
    num_samples: int
    rms: float
    peak: float
    clipping_ratio: float
    spectral_flatness: float


def probe_audio(path: Path) -> AudioProbe:
    """Read a WAV file's header without decoding every sample.

    Args:
        path: Path to a mono 16-bit PCM WAV file.

    Returns:
        :class:`AudioProbe` carrying sample-count, rate, channels,
        sample width, and duration.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        wave.Error: If ``path`` is not a readable WAV file.
    """
    with wave.open(str(path), "rb") as w:
        num_samples = w.getnframes()
        sample_rate = w.getframerate()
        num_channels = w.getnchannels()
        sample_width_bytes = w.getsampwidth()
    duration_sec = num_samples / sample_rate if sample_rate > 0 else 0.0
    return AudioProbe(
        num_samples=num_samples,
        sample_rate=sample_rate,
        num_channels=num_channels,
        sample_width_bytes=sample_width_bytes,
        duration_sec=duration_sec,
    )


def load_samples(path: Path) -> np.ndarray:
    """Decode a mono 16-bit PCM WAV into a float32 array.

    If the file contains more than one channel (unlikely for the
    fixture corpus, but defensive), channels are averaged into a
    single mono track.

    Args:
        path: Path to a 16-bit PCM WAV file.

    Returns:
        1-D float32 array in the ``[-1.0, 1.0]`` range. Empty if the
        file contains no samples.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If the file is not 16-bit PCM.
    """
    with wave.open(str(path), "rb") as w:
        num_channels = w.getnchannels()
        sample_width_bytes = w.getsampwidth()
        num_samples = w.getnframes()
        frames = w.readframes(num_samples)

    if sample_width_bytes != 2:
        raise ValueError(
            f"audio_failure_mode_detectors only supports 16-bit PCM, "
            f"got sample_width={sample_width_bytes} bytes at path={path!s}"
        )

    raw = np.frombuffer(frames, dtype=np.int16)
    if raw.size == 0:
        return np.zeros(0, dtype=np.float32)

    if num_channels > 1:
        raw = raw.reshape(-1, num_channels).mean(axis=1).astype(np.int16)

    return raw.astype(np.float32) / 32768.0


def rms_level(samples: np.ndarray) -> float:
    """Root-mean-square amplitude of a mono float signal.

    Silent audio returns exactly ``0.0``; loud narration typically
    lands in the ``0.1``–``0.3`` range.

    Args:
        samples: 1-D float array in the ``[-1.0, 1.0]`` range.

    Returns:
        RMS amplitude as a float in ``[0.0, 1.0]``. ``0.0`` when
        ``samples`` is empty.
    """
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples))))


def peak_level(samples: np.ndarray) -> float:
    """Maximum absolute sample amplitude.

    ``1.0`` indicates the signal touched the int16 PCM ceiling at
    least once. Near-``1.0`` values are strong evidence of clipping.

    Args:
        samples: 1-D float array in the ``[-1.0, 1.0]`` range.

    Returns:
        Peak absolute amplitude in ``[0.0, 1.0]``. ``0.0`` when
        ``samples`` is empty.
    """
    if samples.size == 0:
        return 0.0
    return float(np.max(np.abs(samples)))


def clipping_ratio(
    samples: np.ndarray,
    *,
    threshold: float = DEFAULT_CLIP_THRESHOLD,
) -> float:
    """Fraction of samples whose absolute value meets ``threshold``.

    Args:
        samples: 1-D float array in the ``[-1.0, 1.0]`` range.
        threshold: Absolute amplitude at or above which a sample
            counts as clipped. Defaults to
            :data:`DEFAULT_CLIP_THRESHOLD`.

    Returns:
        Fraction in ``[0.0, 1.0]``. ``0.0`` when ``samples`` is
        empty.
    """
    if samples.size == 0:
        return 0.0
    clipped = np.sum(np.abs(samples) >= threshold)
    return float(clipped) / float(samples.size)


def spectral_flatness(samples: np.ndarray) -> float:
    """Wiener entropy — geometric/arithmetic mean of magnitude spectrum.

    ``1.0`` means a perfectly flat spectrum (white noise). ``0.0``
    means all the energy is concentrated in a single bin (pure
    tone). Narration typically lands below ``0.3``. The test is
    performed on a single FFT of the entire signal; short signals
    therefore have lower frequency resolution but still produce a
    reliable flatness estimate for the long-vs-narrow distinction
    this evaluator cares about.

    Args:
        samples: 1-D float array in the ``[-1.0, 1.0]`` range.

    Returns:
        Flatness in ``[0.0, 1.0]``. ``0.0`` when ``samples`` is
        empty or the spectrum is entirely zero.
    """
    if samples.size == 0:
        return 0.0

    # Remove DC so a non-zero mean doesn't bias the spectrum toward
    # the 0 Hz bin and flatten every narration clip artificially.
    centered = samples - float(np.mean(samples))
    magnitude = np.abs(np.fft.rfft(centered))
    # Drop the DC bin entirely; it doesn't carry tonal information.
    magnitude = magnitude[1:]
    if magnitude.size == 0:
        return 0.0

    # Protect against log(0); samples exactly at the noise floor
    # contribute nothing meaningful to a flatness estimate.
    eps = 1e-12
    safe = np.maximum(magnitude, eps)
    geometric = float(np.exp(np.mean(np.log(safe))))
    arithmetic = float(np.mean(magnitude))
    if arithmetic <= 0.0:
        return 0.0
    return geometric / arithmetic


def extract_signals(path: Path) -> AudioSignals:
    """Probe + load + compute every primitive into one envelope.

    Args:
        path: Path to a mono 16-bit PCM WAV file.

    Returns:
        :class:`AudioSignals` with duration, rate, sample-count,
        RMS, peak, clipping ratio, and spectral flatness.
    """
    probe = probe_audio(path)
    samples = load_samples(path)
    return AudioSignals(
        path=str(path),
        duration_sec=probe.duration_sec,
        sample_rate=probe.sample_rate,
        num_samples=probe.num_samples,
        rms=rms_level(samples),
        peak=peak_level(samples),
        clipping_ratio=clipping_ratio(samples),
        spectral_flatness=spectral_flatness(samples),
    )


__all__ = [
    "DEFAULT_CLIP_THRESHOLD",
    "AudioProbe",
    "AudioSignals",
    "clipping_ratio",
    "extract_signals",
    "load_samples",
    "peak_level",
    "probe_audio",
    "rms_level",
    "spectral_flatness",
]
