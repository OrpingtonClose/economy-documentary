"""Unit tests for the A/V desync signal-level detector primitives.

Every test feeds a hand-crafted in-memory signal (no fixture I/O,
no ffmpeg invocation) into one of the pure numpy helpers and
asserts the detector produces the expected number. End-to-end
``extract_av_signals`` is exercised separately against the real
committed fixtures in the experiment module.
"""

from __future__ import annotations

import shutil

import numpy as np
import pytest

from strands_agents.evals.evaluators.av_desync_detectors import (
    DEFAULT_AUDIO_ONSET_RMS_THRESHOLD,
    DEFAULT_AUDIO_ONSET_WIN_MS,
    AVProbe,
    AVSignals,
    _parse_rational,
    audio_onset_sec,
)


class TestAudioOnsetSec:
    def test_silence_returns_none(self) -> None:
        samples = np.zeros(16000, dtype=np.float32)
        assert audio_onset_sec(samples, 16000) is None

    def test_sustained_tone_returns_zero_onset(self) -> None:
        samples = np.full(16000, 0.5, dtype=np.float32)
        onset = audio_onset_sec(samples, 16000)
        assert onset == pytest.approx(0.0, abs=1e-6)

    def test_delayed_tone_returns_delayed_onset(self) -> None:
        # 0.5 s of silence, then 0.5 s of 0.5-amp tone.
        samples = np.concatenate(
            [
                np.zeros(8000, dtype=np.float32),
                np.full(8000, 0.5, dtype=np.float32),
            ]
        )
        onset = audio_onset_sec(samples, 16000)
        assert onset is not None
        # Window is 20 ms, onset lands at the first window fully
        # inside the tone segment. Tolerance: one window.
        assert 0.48 <= onset <= 0.52

    def test_empty_samples_returns_none(self) -> None:
        assert audio_onset_sec(np.zeros(0, dtype=np.float32), 16000) is None

    def test_zero_sample_rate_returns_none(self) -> None:
        samples = np.full(1000, 0.5, dtype=np.float32)
        assert audio_onset_sec(samples, 0) is None

    def test_samples_shorter_than_window_returns_none(self) -> None:
        # 5 ms at 16 kHz = 80 samples, window default is 20 ms
        samples = np.full(80, 0.5, dtype=np.float32)
        assert audio_onset_sec(samples, 16000) is None

    def test_below_threshold_returns_none(self) -> None:
        # 0.01 amplitude is below the default 0.02 RMS threshold
        samples = np.full(16000, 0.01, dtype=np.float32)
        assert audio_onset_sec(samples, 16000) is None

    def test_custom_threshold_tightens_gate(self) -> None:
        # Amplitude 0.05 — default passes, custom 0.1 rejects
        samples = np.full(16000, 0.05, dtype=np.float32)
        assert audio_onset_sec(samples, 16000) is not None
        assert audio_onset_sec(samples, 16000, threshold=0.1) is None

    def test_custom_window_ms_changes_cost_not_result(self) -> None:
        # Same constant-amplitude signal, bigger window — onset
        # still 0.0 because the constant crosses any window size.
        samples = np.full(16000, 0.5, dtype=np.float32)
        assert audio_onset_sec(samples, 16000, win_ms=100.0) == pytest.approx(0.0)

    def test_default_threshold_is_pinned(self) -> None:
        # A tiny change to the default would silently shift every
        # experiment case. Pin the current value.
        assert DEFAULT_AUDIO_ONSET_RMS_THRESHOLD == 0.02
        assert DEFAULT_AUDIO_ONSET_WIN_MS == 20.0


class TestParseRational:
    def test_simple_fraction(self) -> None:
        assert _parse_rational("30/1") == 30.0

    def test_ntsc_like_rate(self) -> None:
        assert _parse_rational("30000/1001") == pytest.approx(29.97, abs=0.01)

    def test_plain_float(self) -> None:
        assert _parse_rational("30") == 30.0

    def test_zero_denominator_raises(self) -> None:
        with pytest.raises(ValueError, match="zero denominator"):
            _parse_rational("0/0")

    def test_zero_over_one_is_zero(self) -> None:
        assert _parse_rational("0/1") == 0.0


class TestAVProbeDataclass:
    def test_construction(self) -> None:
        probe = AVProbe(
            duration_sec=2.0,
            has_video_stream=True,
            has_audio_stream=True,
            video_duration_sec=2.0,
            audio_duration_sec=2.0,
            video_fps=15.0,
        )
        assert probe.duration_sec == 2.0
        assert probe.has_video_stream is True
        assert probe.has_audio_stream is True

    def test_is_frozen(self) -> None:
        probe = AVProbe(
            duration_sec=2.0,
            has_video_stream=True,
            has_audio_stream=True,
            video_duration_sec=2.0,
            audio_duration_sec=2.0,
            video_fps=15.0,
        )
        with pytest.raises((AttributeError, TypeError)):
            probe.duration_sec = 3.0  # type: ignore[misc]


class TestAVSignalsDataclass:
    def test_construction_with_full_signals(self) -> None:
        signals = AVSignals(
            path="/tmp/foo.mp4",
            duration_sec=2.0,
            has_video_stream=True,
            has_audio_stream=True,
            audio_onset_sec=0.04,
            video_content_onset_sec=0.0,
            desync_sec=0.04,
            audio_rms=0.07,
        )
        assert signals.desync_sec == pytest.approx(0.04)

    def test_missing_rails_allowed(self) -> None:
        signals = AVSignals(
            path="/tmp/black.mp4",
            duration_sec=2.0,
            has_video_stream=True,
            has_audio_stream=True,
            audio_onset_sec=None,
            video_content_onset_sec=None,
            desync_sec=None,
            audio_rms=0.0,
        )
        assert signals.audio_onset_sec is None
        assert signals.video_content_onset_sec is None
        assert signals.desync_sec is None

    def test_is_frozen(self) -> None:
        signals = AVSignals(
            path="/tmp/foo.mp4",
            duration_sec=2.0,
            has_video_stream=True,
            has_audio_stream=True,
            audio_onset_sec=0.04,
            video_content_onset_sec=0.0,
            desync_sec=0.04,
            audio_rms=0.07,
        )
        with pytest.raises((AttributeError, TypeError)):
            signals.desync_sec = 0.1  # type: ignore[misc]


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH; skipping AV detector end-to-end tests")


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg not on PATH; end-to-end AV detector tests require ffmpeg",
)
class TestFfmpegEndToEnd:
    """End-to-end detector tests against the real committed fixtures.

    Each committed AV fixture has a known desync profile (measured
    empirically when the fixtures were generated). These tests pin
    those values so a future change to the detector primitives or
    to ffmpeg's default decode behaviour is caught immediately.
    """

    def test_synced_fixture_desync_near_zero(self) -> None:
        from pathlib import Path

        from strands_agents.evals.evaluators.av_desync_detectors import (
            extract_av_signals,
        )
        from strands_agents.evals.fixtures.manifest import (
            load_manifest,
            resolve_fixture_path,
        )

        manifest = load_manifest()
        entry = next(
            (e for e in manifest.entries if e.id == "av_synced_narration"),
            None,
        )
        if entry is None:
            pytest.skip("av_synced_narration fixture not present")
        path: Path = resolve_fixture_path(entry)
        signals = extract_av_signals(path)
        assert signals.has_video_stream is True
        assert signals.has_audio_stream is True
        assert signals.audio_onset_sec is not None
        assert signals.video_content_onset_sec is not None
        assert signals.desync_sec is not None
        assert abs(signals.desync_sec) <= 0.15

    def test_audio_ahead_fixture_has_negative_desync(self) -> None:
        from pathlib import Path

        from strands_agents.evals.evaluators.av_desync_detectors import (
            extract_av_signals,
        )
        from strands_agents.evals.fixtures.manifest import (
            load_manifest,
            resolve_fixture_path,
        )

        manifest = load_manifest()
        entry = next(
            (e for e in manifest.entries if e.id == "av_audio_ahead"), None
        )
        if entry is None:
            pytest.skip("av_audio_ahead fixture not present")
        path: Path = resolve_fixture_path(entry)
        signals = extract_av_signals(path)
        assert signals.desync_sec is not None
        assert signals.desync_sec <= -0.3

    def test_audio_behind_fixture_has_positive_desync(self) -> None:
        from pathlib import Path

        from strands_agents.evals.evaluators.av_desync_detectors import (
            extract_av_signals,
        )
        from strands_agents.evals.fixtures.manifest import (
            load_manifest,
            resolve_fixture_path,
        )

        manifest = load_manifest()
        entry = next(
            (e for e in manifest.entries if e.id == "av_audio_behind"), None
        )
        if entry is None:
            pytest.skip("av_audio_behind fixture not present")
        path: Path = resolve_fixture_path(entry)
        signals = extract_av_signals(path)
        assert signals.desync_sec is not None
        assert signals.desync_sec >= 0.3

    def test_audio_missing_fixture_has_no_audio_onset(self) -> None:
        from pathlib import Path

        from strands_agents.evals.evaluators.av_desync_detectors import (
            extract_av_signals,
        )
        from strands_agents.evals.fixtures.manifest import (
            load_manifest,
            resolve_fixture_path,
        )

        manifest = load_manifest()
        entry = next(
            (e for e in manifest.entries if e.id == "av_audio_missing"), None
        )
        if entry is None:
            pytest.skip("av_audio_missing fixture not present")
        path: Path = resolve_fixture_path(entry)
        signals = extract_av_signals(path)
        assert signals.audio_onset_sec is None
        assert signals.audio_rms <= 0.001
        assert signals.desync_sec is None

    def test_video_missing_fixture_has_no_video_onset(self) -> None:
        from pathlib import Path

        from strands_agents.evals.evaluators.av_desync_detectors import (
            extract_av_signals,
        )
        from strands_agents.evals.fixtures.manifest import (
            load_manifest,
            resolve_fixture_path,
        )

        manifest = load_manifest()
        entry = next(
            (e for e in manifest.entries if e.id == "av_video_missing"), None
        )
        if entry is None:
            pytest.skip("av_video_missing fixture not present")
        path: Path = resolve_fixture_path(entry)
        signals = extract_av_signals(path)
        assert signals.video_content_onset_sec is None
        assert signals.desync_sec is None
