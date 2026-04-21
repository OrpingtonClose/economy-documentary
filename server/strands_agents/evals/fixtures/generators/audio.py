"""Deterministic audio-fixture generators.

Audio determinism needed careful plumbing. Both ``sox``-driven synth
(``synth ... whitenoise``, even ``synth ... sine 0``) and ``sox``
re-encodes introduce non-determinism — the output bytes change every
run because sox's synth PRNG is seeded from the clock and its WAV
writer embeds an encoder signature that drifts.

So everything here is pure Python on top of ``numpy`` + the stdlib
``wave`` module:

- Synthetic fixtures (``silence``, ``white_noise``, ``tone``) are
  produced entirely in ``numpy`` with a seeded PRNG, then written
  through ``wave``. Same spec in, byte-identical WAV out.
- Narration is produced by calling ``espeak-ng`` (which IS
  deterministic, confirmed by double-run sha256). The output is read
  back as 16-bit PCM samples and rewritten through ``wave`` to strip
  espeak's header variation.
- ``clipping`` and ``quiet`` apply a linear gain in numpy on top of
  narration samples.

Supported spec kinds:

- ``"narration"`` — espeak-ng reads ``text`` in ``voice`` language,
  resampled through numpy to ``sample_rate``.
- ``"silence"`` — pure zero-amplitude WAV of given duration.
- ``"white_noise"`` — uniform noise drawn from a seeded ``numpy``
  PRNG, scaled to ``amplitude``.
- ``"tone"`` — sine wave at ``freq_hz``.
- ``"clipping"`` — narration boosted to hit peak clipping.
- ``"quiet"`` — narration attenuated by ``gain_db``.
- ``"narration_extended"`` — narration padded with silence or repeated
  to match a target duration. Useful for "narration too long" /
  "narration too short" fixtures.

Every output is 16-bit signed mono PCM at the requested sample rate.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..manifest import compute_sha256

_ESPEAK_BIN = "espeak-ng"
_ESPEAK_NATIVE_RATE = 22050


@dataclass(frozen=True)
class AudioSpec:
    """Typed spec for a single deterministic audio fixture.

    Attributes:
        kind: Generator variant.
        duration_sec: Target duration for synthetic generators.
        sample_rate: Output sample rate. Defaults to 16000 to match
            the pipeline's WhisperX alignment stage.
        extras: Variant-specific knobs.
    """

    kind: str
    duration_sec: float = 2.0
    sample_rate: int = 16000
    extras: dict[str, Any] = field(default_factory=dict)


def generate_audio(spec: AudioSpec, out_path: Path) -> tuple[Path, str]:
    """Render an audio fixture and write it to ``out_path``.

    Returns:
        ``(out_path, sha256_hex)``.

    Raises:
        ValueError: If ``spec.kind`` is unknown.
        RuntimeError: If espeak-ng is required and not found.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    dispatch = {
        "narration": _generate_narration,
        "narration_extended": _generate_narration_extended,
        "silence": _generate_silence,
        "white_noise": _generate_white_noise,
        "tone": _generate_tone,
        "clipping": _generate_clipping,
        "quiet": _generate_quiet,
    }
    try:
        fn = dispatch[spec.kind]
    except KeyError as exc:
        raise ValueError(f"unknown audio spec kind: {spec.kind!r}") from exc

    samples = fn(spec)
    _write_wav(out_path, samples, spec.sample_rate)
    return out_path, compute_sha256(out_path)


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    """Write a 16-bit signed mono PCM WAV deterministically.

    Clips out-of-range samples to int16 bounds. ``samples`` must be a
    1-D float array in the ``[-1.0, 1.0]`` range or an int16 array.
    """
    if samples.dtype != np.int16:
        clipped = np.clip(samples, -1.0, 1.0)
        samples = (clipped * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())


def _resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Simple deterministic linear-interpolation resampler.

    We avoid ``scipy.signal.resample`` to keep the dep graph thin and
    the output stable across numpy / scipy versions.
    """
    if src_rate == dst_rate:
        return samples
    dst_len = int(round(len(samples) * dst_rate / src_rate))
    src_idx = np.linspace(0.0, len(samples) - 1, dst_len)
    base = np.floor(src_idx).astype(np.int64)
    frac = src_idx - base
    base_next = np.clip(base + 1, 0, len(samples) - 1)
    return samples[base] * (1.0 - frac) + samples[base_next] * frac


def _espeak_to_samples(
    text: str,
    voice: str,
    wpm: int,
    pitch: int,
) -> np.ndarray:
    """Run espeak-ng and return 16-bit PCM samples at espeak's native rate."""
    if shutil.which(_ESPEAK_BIN) is None:
        raise RuntimeError(
            "espeak-ng binary not found on PATH — install espeak-ng to regenerate narration"
        )
    result = subprocess.run(  # noqa: S603 — cmd assembled from pinned literals
        [
            _ESPEAK_BIN,
            "-v", voice,
            "-s", str(wpm),
            "-p", str(pitch),
            "--stdout",
            text,
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"espeak-ng failed (code {result.returncode}):\n"
            f"stderr:\n{result.stderr.decode('utf-8', errors='replace')}"
        )

    # Parse the streamed WAV. espeak writes a standard RIFF header
    # followed by PCM data at _ESPEAK_NATIVE_RATE. We grab samples
    # out via ``wave`` from an in-memory BytesIO.
    import io
    with wave.open(io.BytesIO(result.stdout), "rb") as r:
        native_rate = r.getframerate()
        channels = r.getnchannels()
        frames = r.readframes(r.getnframes())

    assert channels == 1, f"expected mono espeak output, got {channels} channels"
    assert native_rate == _ESPEAK_NATIVE_RATE, (
        f"espeak native rate changed: expected {_ESPEAK_NATIVE_RATE}, got {native_rate}"
    )
    return np.frombuffer(frames, dtype=np.int16)


def _generate_narration(spec: AudioSpec) -> np.ndarray:
    text = spec.extras["text"]
    voice = spec.extras.get("voice", "en-us")
    wpm = int(spec.extras.get("wpm", 150))
    pitch = int(spec.extras.get("pitch", 50))

    samples_native = _espeak_to_samples(text, voice, wpm, pitch)
    if spec.sample_rate == _ESPEAK_NATIVE_RATE:
        return samples_native
    # Resample to target rate.
    as_float = samples_native.astype(np.float64) / 32768.0
    return _resample_linear(as_float, _ESPEAK_NATIVE_RATE, spec.sample_rate)


def _generate_narration_extended(spec: AudioSpec) -> np.ndarray:
    """Narration padded or trimmed to exactly ``duration_sec``."""
    base = _generate_narration(spec)
    target_samples = int(round(spec.duration_sec * spec.sample_rate))
    if len(base) == target_samples:
        return base
    if len(base) > target_samples:
        return base[:target_samples]
    # Pad with silence at the end.
    pad = np.zeros(target_samples - len(base), dtype=base.dtype)
    return np.concatenate([base, pad])


def _generate_silence(spec: AudioSpec) -> np.ndarray:
    n = int(round(spec.duration_sec * spec.sample_rate))
    return np.zeros(n, dtype=np.int16)


def _generate_white_noise(spec: AudioSpec) -> np.ndarray:
    seed = int(spec.extras.get("seed", 0))
    amplitude = float(spec.extras.get("amplitude", 0.3))
    rng = np.random.default_rng(seed)
    n = int(round(spec.duration_sec * spec.sample_rate))
    noise = rng.uniform(-amplitude, amplitude, size=n)
    return noise.astype(np.float64)


def _generate_tone(spec: AudioSpec) -> np.ndarray:
    freq_hz = float(spec.extras.get("freq_hz", 440.0))
    amplitude = float(spec.extras.get("amplitude", 0.3))
    n = int(round(spec.duration_sec * spec.sample_rate))
    t = np.arange(n, dtype=np.float64) / spec.sample_rate
    return amplitude * np.sin(2.0 * np.pi * freq_hz * t)


def _generate_clipping(spec: AudioSpec) -> np.ndarray:
    """Boost narration by 4x linear gain to force clipping."""
    narration = _generate_narration(spec)
    if narration.dtype == np.int16:
        narration = narration.astype(np.float64) / 32768.0
    return narration * 4.0


def _generate_quiet(spec: AudioSpec) -> np.ndarray:
    """Attenuate narration by ``gain_db`` (negative)."""
    gain_db = float(spec.extras.get("gain_db", -30.0))
    narration = _generate_narration(spec)
    if narration.dtype == np.int16:
        narration = narration.astype(np.float64) / 32768.0
    linear = 10.0 ** (gain_db / 20.0)
    return narration * linear
