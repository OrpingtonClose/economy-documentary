"""Unit tests for the deterministic AV fixture generator.

Exercises the :func:`generate_av` ffmpeg mux over real source
fixtures. Tests are gated on ffmpeg being available and skip
cleanly when it isn't, so contributors without it set up locally
can still run the rest of the suite.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from strands_agents.evals.fixtures.generators.av import AVSpec, generate_av

_FIXTURES_ROOT = (
    Path(__file__).resolve().parents[2] / "evals" / "fixtures"
)


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH; skipping AV generator tests")


def _source_video() -> Path:
    path = _FIXTURES_ROOT / "video" / "video_hello_red.mp4"
    if not path.exists():
        pytest.skip(f"source video fixture missing: {path}")
    return path


def _source_audio() -> Path:
    path = _FIXTURES_ROOT / "audio" / "audio_english_narration.wav"
    if not path.exists():
        pytest.skip(f"source audio fixture missing: {path}")
    return path


def _source_silence() -> Path:
    path = _FIXTURES_ROOT / "audio" / "audio_silence.wav"
    if not path.exists():
        pytest.skip(f"source silence fixture missing: {path}")
    return path


def _source_black_video() -> Path:
    path = _FIXTURES_ROOT / "video" / "video_black.mp4"
    if not path.exists():
        pytest.skip(f"source black-video fixture missing: {path}")
    return path


class TestGenerateAvSynced:
    def test_produces_mp4_with_both_streams(self, tmp_path: Path) -> None:
        _require_ffmpeg()
        _source_video()
        _source_audio()
        spec = AVSpec(
            video_source="video/video_hello_red.mp4",
            audio_source="audio/audio_english_narration.wav",
            audio_offset_sec=0.0,
        )
        out = tmp_path / "synced.mp4"
        path, sha = generate_av(spec, out, fixtures_root=_FIXTURES_ROOT)
        assert path == out
        assert out.exists()
        assert out.stat().st_size > 0
        # sha256 is 64 hex chars.
        assert len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)

    def test_is_deterministic(self, tmp_path: Path) -> None:
        _require_ffmpeg()
        _source_video()
        _source_audio()
        spec = AVSpec(
            video_source="video/video_hello_red.mp4",
            audio_source="audio/audio_english_narration.wav",
            audio_offset_sec=0.0,
        )
        out_a = tmp_path / "a.mp4"
        out_b = tmp_path / "b.mp4"
        _, sha_a = generate_av(spec, out_a, fixtures_root=_FIXTURES_ROOT)
        _, sha_b = generate_av(spec, out_b, fixtures_root=_FIXTURES_ROOT)
        assert sha_a == sha_b


class TestGenerateAvAudioBehind:
    def test_audio_behind_produces_longer_output(self, tmp_path: Path) -> None:
        _require_ffmpeg()
        _source_video()
        _source_audio()
        spec_sync = AVSpec(
            video_source="video/video_hello_red.mp4",
            audio_source="audio/audio_english_narration.wav",
            audio_offset_sec=0.0,
        )
        spec_behind = AVSpec(
            video_source="video/video_hello_red.mp4",
            audio_source="audio/audio_english_narration.wav",
            audio_offset_sec=0.5,
        )
        path_sync, _ = generate_av(
            spec_sync, tmp_path / "sync.mp4", fixtures_root=_FIXTURES_ROOT
        )
        path_behind, _ = generate_av(
            spec_behind,
            tmp_path / "behind.mp4",
            fixtures_root=_FIXTURES_ROOT,
        )
        assert path_behind.stat().st_size > 0
        # Behind output should be ~0.5s longer than sync. Probe
        # both and compare.
        from strands_agents.evals.evaluators.av_desync_detectors import (
            probe_av,
        )

        probe_sync = probe_av(path_sync)
        probe_behind = probe_av(path_behind)
        assert probe_behind.duration_sec > probe_sync.duration_sec
        # Lower bound: 0.4s (adelay guarantees at least 0.5s but
        # format-level duration round-down can knock off ~50 ms).
        assert probe_behind.duration_sec - probe_sync.duration_sec >= 0.4


class TestGenerateAvAudioAhead:
    def test_audio_ahead_produces_longer_output(self, tmp_path: Path) -> None:
        _require_ffmpeg()
        _source_video()
        _source_audio()
        spec_sync = AVSpec(
            video_source="video/video_hello_red.mp4",
            audio_source="audio/audio_english_narration.wav",
            audio_offset_sec=0.0,
        )
        spec_ahead = AVSpec(
            video_source="video/video_hello_red.mp4",
            audio_source="audio/audio_english_narration.wav",
            audio_offset_sec=-0.5,
        )
        path_sync, _ = generate_av(
            spec_sync, tmp_path / "sync.mp4", fixtures_root=_FIXTURES_ROOT
        )
        path_ahead, _ = generate_av(
            spec_ahead,
            tmp_path / "ahead.mp4",
            fixtures_root=_FIXTURES_ROOT,
        )
        from strands_agents.evals.evaluators.av_desync_detectors import (
            probe_av,
        )

        probe_sync = probe_av(path_sync)
        probe_ahead = probe_av(path_ahead)
        assert probe_ahead.duration_sec > probe_sync.duration_sec
        assert probe_ahead.duration_sec - probe_sync.duration_sec >= 0.4


class TestGenerateAvMissingRails:
    def test_audio_missing_uses_silent_track(self, tmp_path: Path) -> None:
        _require_ffmpeg()
        _source_video()
        _source_silence()
        spec = AVSpec(
            video_source="video/video_hello_red.mp4",
            audio_source="audio/audio_silence.wav",
            audio_offset_sec=0.0,
        )
        path, _ = generate_av(
            spec, tmp_path / "nosound.mp4", fixtures_root=_FIXTURES_ROOT
        )
        from strands_agents.evals.evaluators.av_desync_detectors import (
            extract_av_signals,
        )

        signals = extract_av_signals(path)
        # File still has both streams; the audio stream just
        # carries zero signal.
        assert signals.has_audio_stream is True
        assert signals.audio_rms <= 0.001

    def test_video_missing_uses_black_track(self, tmp_path: Path) -> None:
        _require_ffmpeg()
        _source_black_video()
        _source_audio()
        spec = AVSpec(
            video_source="video/video_black.mp4",
            audio_source="audio/audio_english_narration.wav",
            audio_offset_sec=0.0,
        )
        path, _ = generate_av(
            spec, tmp_path / "black.mp4", fixtures_root=_FIXTURES_ROOT
        )
        from strands_agents.evals.evaluators.av_desync_detectors import (
            extract_av_signals,
        )

        signals = extract_av_signals(path)
        assert signals.has_video_stream is True
        # Black video has no content onset — every frame stays
        # under the luminance threshold.
        assert signals.video_content_onset_sec is None


class TestAVSpecDataclass:
    def test_default_offset_is_zero(self) -> None:
        spec = AVSpec(
            video_source="video/foo.mp4",
            audio_source="audio/foo.wav",
        )
        assert spec.audio_offset_sec == 0.0
        assert spec.extras == {}

    def test_is_frozen(self) -> None:
        spec = AVSpec(
            video_source="video/foo.mp4",
            audio_source="audio/foo.wav",
        )
        with pytest.raises((AttributeError, TypeError)):
            spec.audio_offset_sec = 0.5  # type: ignore[misc]
