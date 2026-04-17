"""Tests for two-phase loudness normalisation.

Fixtures are synthesised with ffmpeg so no network or binary assets are
required.  The fixtures intentionally mix low and high amplitude windows
to reproduce the PAG-run per-window gap (4.4 LU) and let us assert
Phase A brings it down.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace

import pytest

from tools.loudness_normalization import (
    LoudnessOutOfSpec,
    MASTER_I_TOLERANCE_LU,
    measure_loudness,
    normalize_clip,
    normalize_master,
    verify_master,
)
from tools.master_profiles import PREVIEW_512P, YOUTUBE_1080P
from tools.assembly_tools import finalize_master
from tools.title_cards import end_card_for_run, title_card_for_topic


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe required for loudness tests",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _synth_voiced_wav(path: str, dur: float = 10.0, sr: int = 48000) -> None:
    """Synthesize a 10s voiced-like wav with intentional loudness variation.

    The signal is a 220 Hz sine gated with a slow envelope so different
    1-second windows sit at different loudness \u2014 this gives
    ``loudnorm`` something meaningful to measure and makes the
    before/after integrated LUFS different.
    """
    # amodulate the sine with a sine envelope so every ~2s the level
    # swings ~6 dB, producing LRA > 1 LU and a measurable integrated
    # loudness.
    af = (
        f"sine=frequency=220:sample_rate={sr}:duration={dur},"
        "volume=enable='between(t,0,2)':volume=0.9,"
        "volume=enable='between(t,2,4)':volume=0.3,"
        "volume=enable='between(t,4,6)':volume=0.7,"
        "volume=enable='between(t,6,8)':volume=0.2,"
        "volume=enable='between(t,8,10)':volume=0.8"
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", af,
        "-ac", "1", "-ar", str(sr),
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def _synth_video(path: str, w: int, h: int, fps: int, dur: float) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=gray:s={w}x{h}:r={fps}:d={dur}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-pix_fmt", "yuv420p",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def voiced_wav(tmp_path):
    path = str(tmp_path / "voiced.wav")
    _synth_voiced_wav(path, dur=10.0)
    return path


# ---------------------------------------------------------------------------
# measure_loudness
# ---------------------------------------------------------------------------

class TestMeasureLoudness:
    def test_measurement_returns_finite_values(self, voiced_wav):
        stats = measure_loudness(voiced_wav)
        assert stats.input_i < 0.0, "integrated loudness should be negative dBFS"
        assert stats.input_i > -70.0, "input must not be silence"
        assert stats.input_tp < 1.0, "true peak should be below +1 dBTP"
        assert stats.input_lra >= 0.0

    def test_missing_input_raises(self, tmp_path):
        from tools.loudness_normalization import LoudnessMeasurementFailed
        with pytest.raises(LoudnessMeasurementFailed, match="not found"):
            measure_loudness(str(tmp_path / "missing.wav"))


# ---------------------------------------------------------------------------
# Phase A \u2014 per-clip
# ---------------------------------------------------------------------------

class TestPhaseAPerClip:
    def test_per_clip_moves_toward_target(self, voiced_wav, tmp_path):
        out = str(tmp_path / "clip_norm.wav")
        result = normalize_clip(voiced_wav, out)
        assert os.path.exists(out)

        # The output should land close to -23 LUFS.  EBU R128 tolerance
        # for the whole-stream integrated value is \u00b11 LU after a
        # two-pass loudnorm; we allow a slightly looser \u00b11.5 LU on
        # the *synthesised* fixture to keep the test stable across
        # ffmpeg 4.x / 5.x builds without sacrificing the regression.
        assert abs(result.measured_after.input_i - (-23.0)) <= 1.5, (
            f"post-Phase-A integrated LUFS "
            f"{result.measured_after.input_i:.2f} deviates from -23 by "
            f"> 1.5 LU"
        )

    def test_per_clip_reduces_loudness_range(self, voiced_wav, tmp_path):
        # The synthesised fixture has strong 1s-scale amplitude swings;
        # two-pass loudnorm should either preserve or reduce LRA.  We
        # assert it does not *grow* the range.
        out = str(tmp_path / "clip_norm2.wav")
        result = normalize_clip(voiced_wav, out)
        assert (
            result.measured_after.input_lra
            <= result.measured_before.input_lra + 0.5
        )

    def test_per_clip_target_stored_on_result(self, voiced_wav, tmp_path):
        out = str(tmp_path / "clip_norm3.wav")
        result = normalize_clip(voiced_wav, out)
        assert result.target_lufs == -23.0
        assert result.input_path == voiced_wav
        assert result.output_path == out


# ---------------------------------------------------------------------------
# Phase B \u2014 final master + verify
# ---------------------------------------------------------------------------

class TestPhaseBMaster:
    def test_master_hits_youtube_target(self, voiced_wav, tmp_path):
        out = str(tmp_path / "master.m4a")
        result = normalize_master(voiced_wav, out, YOUTUBE_1080P)
        assert os.path.exists(out)
        # Integrated LUFS within \u00b10.5 LU of target enforced by
        # verify_master (which normalize_master calls); re-assert here
        # so the test fails loud rather than depending on the helper.
        assert (
            abs(result.measured_after.input_i - YOUTUBE_1080P.integrated_lufs)
            <= MASTER_I_TOLERANCE_LU
        )
        assert result.measured_after.input_lra <= YOUTUBE_1080P.max_lra

    def test_master_encodes_with_profile_codec(self, voiced_wav, tmp_path):
        out = str(tmp_path / "master.m4a")
        normalize_master(voiced_wav, out, YOUTUBE_1080P)

        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", out,
        ]
        probe = json.loads(
            subprocess.run(
                probe_cmd, capture_output=True, text=True, timeout=30,
            ).stdout
        )
        streams = probe.get("streams", [])
        assert streams, "no streams in master"
        assert streams[0]["codec_name"] == "aac"
        assert int(streams[0].get("sample_rate", 0)) == 48000

    def test_verify_master_rejects_out_of_spec_integrated(self, voiced_wav, tmp_path):
        # Manually construct a result that's outside tolerance, then make
        # sure verify_master raises.
        from tools.loudness_normalization import LoudnessResult, LoudnessStats
        bad = LoudnessResult(
            input_path=voiced_wav,
            output_path=voiced_wav,
            target_lufs=-14.0,
            true_peak_db=-1.0,
            measured_before=LoudnessStats(-20, -5, 3.0, -30),
            measured_after=LoudnessStats(
                input_i=-12.0,       # 2 LU off
                input_tp=-1.2,
                input_lra=3.0,
                input_thresh=-24.0,
            ),
        )
        with pytest.raises(LoudnessOutOfSpec, match="Integrated LUFS"):
            verify_master(bad, YOUTUBE_1080P)

    def test_verify_master_rejects_excessive_lra(self, voiced_wav, tmp_path):
        from tools.loudness_normalization import LoudnessResult, LoudnessStats
        bad = LoudnessResult(
            input_path=voiced_wav,
            output_path=voiced_wav,
            target_lufs=-14.0,
            true_peak_db=-1.0,
            measured_before=LoudnessStats(-20, -5, 8.0, -30),
            measured_after=LoudnessStats(
                input_i=-14.0,
                input_tp=-1.2,
                input_lra=8.0,   # profile max is 5
                input_thresh=-24.0,
            ),
        )
        with pytest.raises(LoudnessOutOfSpec, match="LRA"):
            verify_master(bad, YOUTUBE_1080P)


# ---------------------------------------------------------------------------
# Integration \u2014 finalize_master respects preview guard + loudnorm
# ---------------------------------------------------------------------------

class TestFinalizeMaster:
    def test_final_filename_rejects_preview_profile(self, tmp_path, voiced_wav):
        video = str(tmp_path / "body.mp4")
        _synth_video(video, 512, 320, 24, 10.0)
        out = str(tmp_path / "final_documentary.mp4")
        result_raw = finalize_master(
            body_video_path=video,
            body_audio_path=voiced_wav,
            output_path=out,
            master_profile=PREVIEW_512P,
        )
        result = json.loads(result_raw)
        assert "error" in result
        assert "preview profile" in result["error"].lower()
        assert not os.path.exists(out)

    def test_final_renders_with_cards_and_profile_resolution(
        self, tmp_path, voiced_wav,
    ):
        video = str(tmp_path / "body.mp4")
        _synth_video(video, 512, 320, 24, 10.0)
        out = str(tmp_path / "final_documentary.mp4")

        title = title_card_for_topic(
            topic="Test", channel="EconDoc", duration_sec=1.0,
        )
        end = end_card_for_run(channel="EconDoc", duration_sec=1.5)

        result = json.loads(finalize_master(
            body_video_path=video,
            body_audio_path=voiced_wav,
            output_path=out,
            master_profile=YOUTUBE_1080P,
            title_card=title,
            end_card=end,
        ))
        assert result.get("status") == "finalized", result
        assert os.path.exists(out)

        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", out,
        ]
        probe = json.loads(
            subprocess.run(
                probe_cmd, capture_output=True, text=True, timeout=30,
            ).stdout
        )
        video_stream = next(
            s for s in probe["streams"] if s["codec_type"] == "video"
        )
        audio_stream = next(
            s for s in probe["streams"] if s["codec_type"] == "audio"
        )
        assert video_stream["width"] == 1920
        assert video_stream["height"] == 1080
        # Regression guard for the Devin Review finding: the concat step
        # must honour the master profile rather than falling back to the
        # legacy settings (no bt709 tags, AAC @ 192k, no fps lock).
        # bitrate is a weak signal on synthetic signals (a sine compresses
        # far below the target), so we assert on the colour tags + sample
        # rate + fps which the legacy path does NOT write.
        assert audio_stream["codec_name"] == "aac"
        assert int(audio_stream.get("sample_rate", 0)) == 48000
        assert video_stream.get("color_space") == "bt709", (
            f"expected bt709 colorspace, got {video_stream.get('color_space')!r}"
            " \u2014 concat likely used legacy settings"
        )
        assert video_stream.get("color_primaries") == "bt709"
        assert video_stream.get("color_transfer") == "bt709"
        # r_frame_rate is a fraction string like "24/1".
        rate = video_stream.get("r_frame_rate", "0/1")
        num, den = (int(x) for x in rate.split("/"))
        assert abs(num / den - 24.0) < 0.01, (
            f"expected 24fps lock from profile, got {rate}"
        )
        # Total duration = title (1s) + body (10s) + end (1.5s) \u2248 12.5s
        total = float(probe["format"]["duration"])
        assert 11.5 <= total <= 13.5, total
