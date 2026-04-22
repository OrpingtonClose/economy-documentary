"""Unit tests for the audio failure-mode detector primitives.

Every test feeds a hand-crafted in-memory signal (no fixture I/O)
into one of the pure numpy functions and asserts the detector
produces the expected number. The goal is to pin the detectors'
behaviour on edge cases so future threshold tuning can't silently
regress the failure-mode experiment.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from strands_agents.evals.evaluators.audio_failure_mode_detectors import (
    DEFAULT_CLIP_THRESHOLD,
    AudioProbe,
    AudioSignals,
    clipping_ratio,
    extract_signals,
    load_samples,
    peak_level,
    probe_audio,
    rms_level,
    spectral_flatness,
)


def _write_wav(path: Path, samples: np.ndarray, *, sample_rate: int = 16000) -> Path:
    """Write a float array as mono 16-bit PCM WAV."""
    int_samples = np.clip(samples, -1.0, 1.0)
    int_samples = (int_samples * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(int_samples.tobytes())
    return path


class TestRmsLevel:
    def test_silence_rms_is_zero(self) -> None:
        assert rms_level(np.zeros(1000, dtype=np.float32)) == 0.0

    def test_constant_rms_matches_amplitude(self) -> None:
        samples = np.full(1000, 0.5, dtype=np.float32)
        assert rms_level(samples) == pytest.approx(0.5, abs=1e-6)

    def test_empty_array_returns_zero(self) -> None:
        assert rms_level(np.zeros(0, dtype=np.float32)) == 0.0

    def test_sinusoid_rms_is_amplitude_over_sqrt_two(self) -> None:
        t = np.arange(16000, dtype=np.float32) / 16000.0
        samples = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        assert rms_level(samples) == pytest.approx(1.0 / np.sqrt(2.0), abs=1e-3)


class TestPeakLevel:
    def test_silence_peak_is_zero(self) -> None:
        assert peak_level(np.zeros(100, dtype=np.float32)) == 0.0

    def test_peak_returns_max_absolute(self) -> None:
        samples = np.array([0.1, -0.4, 0.2, -0.7, 0.3], dtype=np.float32)
        assert peak_level(samples) == pytest.approx(0.7)

    def test_empty_array_returns_zero(self) -> None:
        assert peak_level(np.zeros(0, dtype=np.float32)) == 0.0

    def test_peak_at_rail(self) -> None:
        samples = np.array([0.0, 1.0, -1.0], dtype=np.float32)
        assert peak_level(samples) == pytest.approx(1.0)


class TestClippingRatio:
    def test_silence_has_zero_clipping(self) -> None:
        assert clipping_ratio(np.zeros(1000, dtype=np.float32)) == 0.0

    def test_all_clipped(self) -> None:
        samples = np.ones(100, dtype=np.float32)
        assert clipping_ratio(samples) == pytest.approx(1.0)

    def test_empty_array_returns_zero(self) -> None:
        assert clipping_ratio(np.zeros(0, dtype=np.float32)) == 0.0

    def test_half_clipped(self) -> None:
        samples = np.concatenate(
            [
                np.full(500, 1.0, dtype=np.float32),
                np.full(500, 0.5, dtype=np.float32),
            ]
        )
        assert clipping_ratio(samples) == pytest.approx(0.5)

    def test_threshold_exclusivity(self) -> None:
        # A sample exactly at the default threshold counts as clipped.
        samples = np.array([DEFAULT_CLIP_THRESHOLD] * 10, dtype=np.float32)
        assert clipping_ratio(samples) == pytest.approx(1.0)

    def test_custom_threshold(self) -> None:
        samples = np.array([0.85, 0.85, 0.10, 0.10], dtype=np.float32)
        assert clipping_ratio(samples, threshold=0.8) == pytest.approx(0.5)


class TestSpectralFlatness:
    def test_silence_flatness_is_zero(self) -> None:
        assert spectral_flatness(np.zeros(1000, dtype=np.float32)) == 0.0

    def test_pure_tone_flatness_is_low(self) -> None:
        t = np.arange(16000, dtype=np.float32) / 16000.0
        samples = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        # A pure sinusoid concentrates energy in one bin — flatness
        # must be very near zero.
        assert spectral_flatness(samples) < 0.05

    def test_white_noise_flatness_is_high(self) -> None:
        rng = np.random.default_rng(seed=42)
        samples = rng.uniform(-0.5, 0.5, size=16000).astype(np.float32)
        # White noise should spread energy evenly — flatness well
        # above the 0.7 threshold used by the evaluator.
        assert spectral_flatness(samples) > 0.7

    def test_empty_array_returns_zero(self) -> None:
        assert spectral_flatness(np.zeros(0, dtype=np.float32)) == 0.0

    def test_dc_only_signal_returns_zero(self) -> None:
        # A constant signal has all its energy at DC, which the
        # detector strips; remaining bins are all zero.
        samples = np.full(4096, 0.5, dtype=np.float32)
        assert spectral_flatness(samples) == 0.0


class TestProbeAudio:
    def test_probe_returns_header_fields(self, tmp_path: Path) -> None:
        path = _write_wav(
            tmp_path / "a.wav",
            np.zeros(16000, dtype=np.float32),
            sample_rate=16000,
        )
        probe = probe_audio(path)
        assert isinstance(probe, AudioProbe)
        assert probe.num_samples == 16000
        assert probe.sample_rate == 16000
        assert probe.num_channels == 1
        assert probe.sample_width_bytes == 2
        assert probe.duration_sec == pytest.approx(1.0)

    def test_probe_zero_length_duration(self, tmp_path: Path) -> None:
        path = _write_wav(
            tmp_path / "empty.wav",
            np.zeros(0, dtype=np.float32),
            sample_rate=16000,
        )
        probe = probe_audio(path)
        assert probe.num_samples == 0
        assert probe.duration_sec == 0.0

    def test_probe_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            probe_audio(tmp_path / "does_not_exist.wav")


class TestLoadSamples:
    def test_load_returns_float_range(self, tmp_path: Path) -> None:
        original = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
        path = _write_wav(tmp_path / "a.wav", original)
        loaded = load_samples(path)
        assert loaded.dtype == np.float32
        assert loaded.shape == (5,)
        assert loaded.max() <= 1.0
        assert loaded.min() >= -1.0
        # Quantisation to int16 will shift amplitudes slightly.
        np.testing.assert_allclose(loaded, original, atol=1e-4)

    def test_load_empty_returns_empty_array(self, tmp_path: Path) -> None:
        path = _write_wav(tmp_path / "empty.wav", np.zeros(0, dtype=np.float32))
        loaded = load_samples(path)
        assert loaded.size == 0
        assert loaded.dtype == np.float32

    def test_load_rejects_non_16bit(self, tmp_path: Path) -> None:
        path = tmp_path / "b.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(1)  # 8-bit is not supported
            w.setframerate(8000)
            w.writeframes(b"\x80" * 100)
        with pytest.raises(ValueError, match="16-bit PCM"):
            load_samples(path)


class TestExtractSignals:
    def test_extract_signals_envelope_shape(self, tmp_path: Path) -> None:
        rng = np.random.default_rng(seed=7)
        samples = rng.uniform(-0.2, 0.2, size=8000).astype(np.float32)
        path = _write_wav(tmp_path / "mix.wav", samples, sample_rate=8000)

        signals = extract_signals(path)
        assert isinstance(signals, AudioSignals)
        assert signals.path == str(path)
        assert signals.sample_rate == 8000
        assert signals.num_samples == 8000
        assert signals.duration_sec == pytest.approx(1.0)
        assert 0.0 <= signals.rms <= 1.0
        assert 0.0 <= signals.peak <= 1.0
        assert 0.0 <= signals.clipping_ratio <= 1.0
        assert 0.0 <= signals.spectral_flatness <= 1.0

    def test_silence_fixture_shape(self, tmp_path: Path) -> None:
        path = _write_wav(
            tmp_path / "silence.wav",
            np.zeros(16000, dtype=np.float32),
        )
        signals = extract_signals(path)
        assert signals.rms == 0.0
        assert signals.peak == 0.0
        assert signals.clipping_ratio == 0.0

    def test_clipped_fixture_shape(self, tmp_path: Path) -> None:
        samples = np.concatenate(
            [
                np.full(4000, 1.0, dtype=np.float32),
                np.full(12000, 0.3, dtype=np.float32),
            ]
        )
        path = _write_wav(tmp_path / "clipped.wav", samples)
        signals = extract_signals(path)
        assert signals.peak >= DEFAULT_CLIP_THRESHOLD
        assert signals.clipping_ratio > 0.2
