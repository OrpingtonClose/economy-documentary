"""Unit tests for :mod:`strands_agents.qa_gates`.

Synthetic MP4s + WAVs are generated with ffmpeg fixtures so the
suite stays hermetic — no GPU, no network, no model credentials.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from strands_agents import qa_gates
from strands_agents.qa_gates import (
    DEFAULT_DURATION_TOLERANCE_S,
    DEFAULT_MIN_MEAN_PIXEL_DELTA,
    MIN_TRAILING_SILENCE_S,
    VERDICT_FAIL,
    VERDICT_PASS,
    qa_audio_completeness,
    qa_duration_align,
    qa_stills_judge,
    qa_video_artifact_probe,
)


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available",
)


def _invoke(tool: Any, **kwargs: Any) -> dict[str, Any]:
    """Call a ``@tool``-decorated function via its langchain handle."""
    return tool.invoke(kwargs)


def _make_silent_wav(path: Path, duration_s: float) -> None:
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=mono:sample_rate=16000",
        "-t",
        f"{duration_s}",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _make_natural_narration_wav(
    path: Path,
    *,
    speech_duration_s: float = 2.0,
    trailing_silence_s: float = 0.4,
) -> None:
    """Synthesise a healthy narration: speech then trailing silence.

    Speech is a sine tone at -10 dBFS for ``speech_duration_s``, then
    pure silence for ``trailing_silence_s``. This mirrors the
    auditory shape of a natural sentence ending: spoken energy that
    decays into room tone before the file ends.
    """
    speech = path.with_suffix(".speech.wav")
    silence = path.with_suffix(".silence.wav")
    list_file = path.with_suffix(".list")
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            f"{speech_duration_s}",
            "-af",
            "volume=-10dB",
            "-ac",
            "1",
            str(speech),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=mono:sample_rate=16000",
            "-t",
            f"{trailing_silence_s}",
            str(silence),
        ],
        check=True,
        capture_output=True,
    )
    list_file.write_text(f"file '{speech}'\nfile '{silence}'\n")
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_abrupt_cut_wav(path: Path, *, duration_s: float = 2.0) -> None:
    """Synthesise an abruptly-cut narration: full-amplitude tone to EOF.

    A 0-dBFS sine wave for the whole duration with no trailing
    silence — the auditory signature of Qwen3-TTS running out of
    token budget mid-utterance. Tail RMS lands around -3 dBFS,
    well above the speech-band threshold; combined with zero
    trailing silence, both auditory signatures flag a hard cut.
    """
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=16000",
        "-t",
        f"{duration_s}",
        "-ac",
        "1",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _make_motion_mp4(path: Path, duration_s: float, fps: int = 24) -> None:
    """Render a coloured-noise MP4 with strong inter-frame motion."""
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"mandelbrot=size=128x128:rate={fps}",
        "-t",
        f"{duration_s}",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _make_still_mp4(path: Path, duration_s: float, fps: int = 24) -> None:
    """Render an MP4 that is a single solid colour (zero motion)."""
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=color=gray:size=128x128:rate={fps}",
        "-t",
        f"{duration_s}",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# qa_video_artifact_probe
# ---------------------------------------------------------------------------


class TestQaVideoArtifactProbe:
    def test_pass_on_valid_motion_clip(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        _make_motion_mp4(video, 4.0)

        result = _invoke(
            qa_video_artifact_probe,
            scene_id="scene_1",
            video_path=str(video),
        )

        assert result["tool"] == "qa_video_artifact_probe"
        assert result["verdict"] == VERDICT_PASS
        assert result["scene_id"] == "scene_1"
        assert result["duration_s"] == pytest.approx(4.0, abs=0.5)
        assert result["width"] == 128
        assert result["height"] == 128
        assert result["codec"] == "h264"
        assert result["size_bytes"] > 1024

    def test_fail_when_video_missing(self, tmp_path: Path) -> None:
        result = _invoke(
            qa_video_artifact_probe,
            scene_id="scene_1",
            video_path=str(tmp_path / "missing.mp4"),
        )
        assert result["verdict"] == VERDICT_FAIL
        assert "does not exist" in result["error"]

    def test_fail_when_video_too_small(self, tmp_path: Path) -> None:
        path = tmp_path / "tiny.mp4"
        path.write_bytes(b"x")
        result = _invoke(
            qa_video_artifact_probe,
            scene_id="scene_1",
            video_path=str(path),
        )
        assert result["verdict"] == VERDICT_FAIL
        assert "too small" in result["error"]


# ---------------------------------------------------------------------------
# qa_duration_align
# ---------------------------------------------------------------------------


class TestQaDurationAlign:
    def test_pass_when_durations_match(self, tmp_path: Path) -> None:
        audio = tmp_path / "a.wav"
        video = tmp_path / "v.mp4"
        _make_silent_wav(audio, 4.0)
        _make_motion_mp4(video, 4.0)

        result = _invoke(
            qa_duration_align,
            scene_id="scene_1",
            audio_path=str(audio),
            video_path=str(video),
        )

        assert result["tool"] == "qa_duration_align"
        assert result["verdict"] == VERDICT_PASS
        # delta_s is post-mux, always 0 by construction (loop-fill).
        assert result["delta_s"] == pytest.approx(0.0, abs=1e-6)
        assert result["pre_mux_delta_s"] == pytest.approx(0.0, abs=0.2)
        assert result["loop_factor"] == pytest.approx(1.0, abs=0.05)
        assert result["tolerance_s"] == DEFAULT_DURATION_TOLERANCE_S

    def test_pass_when_video_shorter_than_audio_within_loop_factor(
        self, tmp_path: Path
    ) -> None:
        # The slice 9j regression (13 s narration + 3.7 s clip) is
        # PASS after PR #375 because the assembly leaf loops the clip
        # to fill audio. ``loop_factor = 13/3.7 ≈ 3.5×`` is well below
        # the default 5.0× ceiling, so this is a watchable scene.
        audio = tmp_path / "a.wav"
        video = tmp_path / "v.mp4"
        _make_silent_wav(audio, 13.0)
        _make_motion_mp4(video, 3.7)

        result = _invoke(
            qa_duration_align,
            scene_id="scene_1",
            audio_path=str(audio),
            video_path=str(video),
        )

        assert result["verdict"] == VERDICT_PASS
        assert result["loop_factor"] == pytest.approx(13.0 / 3.7, abs=0.1)
        assert result["looped_video_duration_s"] == pytest.approx(13.0, abs=0.2)
        assert result["pre_mux_delta_s"] == pytest.approx(9.3, abs=0.5)

    def test_fail_when_loop_factor_exceeds_ceiling(
        self, tmp_path: Path
    ) -> None:
        # 1 s clip + 12 s narration => loop_factor = 12× > 5× ceiling.
        # Fail-closed: too repetitive to call documentary footage.
        audio = tmp_path / "a.wav"
        video = tmp_path / "v.mp4"
        _make_silent_wav(audio, 12.0)
        _make_motion_mp4(video, 1.0)

        result = _invoke(
            qa_duration_align,
            scene_id="scene_1",
            audio_path=str(audio),
            video_path=str(video),
        )

        assert result["verdict"] == VERDICT_FAIL
        assert "loop_factor" in result["reason"]
        assert result["loop_factor"] == pytest.approx(12.0, abs=0.5)

    def test_fail_when_video_too_short(self, tmp_path: Path) -> None:
        # Sub-half-second video => degenerate clip (likely LTX OOM).
        # Looping it can't hide that.
        audio = tmp_path / "a.wav"
        video = tmp_path / "v.mp4"
        _make_silent_wav(audio, 4.0)
        _make_motion_mp4(video, 0.3)

        result = _invoke(
            qa_duration_align,
            scene_id="scene_1",
            audio_path=str(audio),
            video_path=str(video),
        )

        assert result["verdict"] == VERDICT_FAIL
        assert "min_video_duration_s" in result["reason"]

    def test_pass_within_default_tolerance(self, tmp_path: Path) -> None:
        # Was a tolerance-based PASS pre-PR#375; remains PASS post.
        audio = tmp_path / "a.wav"
        video = tmp_path / "v.mp4"
        _make_silent_wav(audio, 4.0)
        _make_motion_mp4(video, 4.3)

        result = _invoke(
            qa_duration_align,
            scene_id="scene_1",
            audio_path=str(audio),
            video_path=str(video),
        )

        assert result["verdict"] == VERDICT_PASS

    def test_custom_max_loop_factor_overrides_default(
        self, tmp_path: Path
    ) -> None:
        # 4 s narration + 0.6 s clip => loop_factor ≈ 6.7×.
        # Default 5.0× ceiling fails; a 7× override passes.
        audio = tmp_path / "a.wav"
        video = tmp_path / "v.mp4"
        _make_silent_wav(audio, 4.0)
        _make_motion_mp4(video, 0.6)

        default_result = _invoke(
            qa_duration_align,
            scene_id="scene_1",
            audio_path=str(audio),
            video_path=str(video),
        )
        assert default_result["verdict"] == VERDICT_FAIL

        loose_result = _invoke(
            qa_duration_align,
            scene_id="scene_1",
            audio_path=str(audio),
            video_path=str(video),
            max_loop_factor=7.0,
        )
        assert loose_result["verdict"] == VERDICT_PASS

    def test_fail_when_audio_missing(self, tmp_path: Path) -> None:
        video = tmp_path / "v.mp4"
        _make_motion_mp4(video, 4.0)

        result = _invoke(
            qa_duration_align,
            scene_id="scene_1",
            audio_path=str(tmp_path / "missing.wav"),
            video_path=str(video),
        )
        assert result["verdict"] == VERDICT_FAIL
        assert "audio_path does not exist" in result["error"]

    def test_fail_when_video_missing(self, tmp_path: Path) -> None:
        audio = tmp_path / "a.wav"
        _make_silent_wav(audio, 4.0)

        result = _invoke(
            qa_duration_align,
            scene_id="scene_1",
            audio_path=str(audio),
            video_path=str(tmp_path / "missing.mp4"),
        )
        assert result["verdict"] == VERDICT_FAIL
        assert "video_path does not exist" in result["error"]


# ---------------------------------------------------------------------------
# qa_stills_judge
# ---------------------------------------------------------------------------


class TestQaStillsJudge:
    def test_fail_on_solid_colour_clip(self, tmp_path: Path) -> None:
        # A solid grey clip has zero motion — must hard-fail.
        video = tmp_path / "still.mp4"
        _make_still_mp4(video, 4.0)

        result = _invoke(
            qa_stills_judge,
            scene_id="scene_1",
            video_path=str(video),
        )

        assert result["tool"] == "qa_stills_judge"
        assert result["verdict"] == VERDICT_FAIL
        assert result["mean_pixel_delta"] < DEFAULT_MIN_MEAN_PIXEL_DELTA
        assert "mean inter-frame" in result["reason"]

    def test_pass_on_motion_clip(self, tmp_path: Path) -> None:
        video = tmp_path / "motion.mp4"
        _make_motion_mp4(video, 4.0)

        result = _invoke(
            qa_stills_judge,
            scene_id="scene_1",
            video_path=str(video),
        )

        assert result["verdict"] == VERDICT_PASS
        assert result["mean_pixel_delta"] >= DEFAULT_MIN_MEAN_PIXEL_DELTA

    def test_fail_when_video_missing(self, tmp_path: Path) -> None:
        result = _invoke(
            qa_stills_judge,
            scene_id="scene_1",
            video_path=str(tmp_path / "missing.mp4"),
        )
        assert result["verdict"] == VERDICT_FAIL
        assert "does not exist" in result["error"]

    def test_num_samples_minimum_enforced(self, tmp_path: Path) -> None:
        video = tmp_path / "v.mp4"
        _make_motion_mp4(video, 4.0)

        with pytest.raises(ValueError):
            _invoke(
                qa_stills_judge,
                scene_id="scene_1",
                video_path=str(video),
                num_samples=1,
            )

    def test_custom_floor_overrides_default(self, tmp_path: Path) -> None:
        # Motion clip's delta is well above 1.5; bump the floor to a
        # value above its delta and we should fail.
        video = tmp_path / "v.mp4"
        _make_motion_mp4(video, 4.0)

        # Sanity: default passes
        baseline = _invoke(
            qa_stills_judge,
            scene_id="scene_1",
            video_path=str(video),
        )
        assert baseline["verdict"] == VERDICT_PASS

        # Bump floor far above any plausible motion delta -> fail
        high_floor = _invoke(
            qa_stills_judge,
            scene_id="scene_1",
            video_path=str(video),
            min_mean_pixel_delta=200.0,
        )
        assert high_floor["verdict"] == VERDICT_FAIL


# ---------------------------------------------------------------------------
# qa_audio_completeness  (slice 9p — auditory abrupt-cut detection)
# ---------------------------------------------------------------------------


class TestQaAudioCompleteness:
    def test_pass_on_natural_narration(self, tmp_path: Path) -> None:
        # Speech + 400 ms trailing silence → both auditory tests pass.
        audio = tmp_path / "natural.wav"
        _make_natural_narration_wav(
            audio, speech_duration_s=2.0, trailing_silence_s=0.4
        )

        result = _invoke(
            qa_audio_completeness,
            scene_id="scene_1",
            audio_path=str(audio),
        )

        assert result["tool"] == "qa_audio_completeness"
        assert result["verdict"] == VERDICT_PASS
        assert result["trailing_silence_s"] >= MIN_TRAILING_SILENCE_S
        assert result["audio_duration_s"] == pytest.approx(2.4, abs=0.2)

    def test_fail_on_abrupt_cut(self, tmp_path: Path) -> None:
        # Pure speech-band tone right up to the file boundary → both
        # auditory tests fail (no trailing silence + high tail RMS).
        audio = tmp_path / "abrupt.wav"
        _make_abrupt_cut_wav(audio, duration_s=2.0)

        result = _invoke(
            qa_audio_completeness,
            scene_id="scene_1",
            audio_path=str(audio),
        )

        assert result["verdict"] == VERDICT_FAIL
        assert result["trailing_silence_s"] < MIN_TRAILING_SILENCE_S
        assert "reason" in result
        # At least one of the two auditory signatures must be cited.
        assert (
            "trailing silence" in result["reason"]
            or "end-of-file RMS" in result["reason"]
        )

    def test_pass_on_short_silence_with_quiet_tail(self, tmp_path: Path) -> None:
        # Real Qwen3-TTS narrations frequently leave only ~80 ms of
        # detectable trailing silence but the tail is essentially
        # digital silence (-100 dBFS+). That's healthy: either
        # auditory signature alone clears the file. The combined
        # gate is "fail only if both signals flag a cut".
        audio = tmp_path / "short_tail.wav"
        _make_natural_narration_wav(
            audio, speech_duration_s=2.0, trailing_silence_s=0.05
        )

        result = _invoke(
            qa_audio_completeness,
            scene_id="scene_1",
            audio_path=str(audio),
        )

        assert result["verdict"] == VERDICT_PASS
        # Silence test alone fails (50 ms < 150 ms floor) but RMS test
        # passes — file ends in silence even though the silencedetect
        # window measured a shorter region.
        assert result["trailing_silence_s"] < MIN_TRAILING_SILENCE_S

    def test_loose_thresholds_can_clear_an_abrupt_cut(
        self, tmp_path: Path
    ) -> None:
        # Operators can loosen the gate per-call: an abrupt cut
        # (both signatures fail at default) becomes PASS once the
        # silence floor is lowered below 0 s. Demonstrates the
        # thresholds are wired through and tunable.
        audio = tmp_path / "abrupt.wav"
        _make_abrupt_cut_wav(audio, duration_s=2.0)

        default = _invoke(
            qa_audio_completeness,
            scene_id="scene_1",
            audio_path=str(audio),
        )
        assert default["verdict"] == VERDICT_FAIL

        loosened = _invoke(
            qa_audio_completeness,
            scene_id="scene_1",
            audio_path=str(audio),
            min_trailing_silence_s=-1.0,
            max_tail_rms_db=10.0,
        )
        assert loosened["verdict"] == VERDICT_PASS

    def test_fail_when_audio_missing(self, tmp_path: Path) -> None:
        result = _invoke(
            qa_audio_completeness,
            scene_id="scene_1",
            audio_path=str(tmp_path / "missing.wav"),
        )
        assert result["verdict"] == VERDICT_FAIL
        assert "does not exist" in result["error"]

    def test_fail_when_audio_too_short(self, tmp_path: Path) -> None:
        # 100 ms file is below the 0.5 s floor → fail loudly rather
        # than rendering an inscrutable verdict.
        audio = tmp_path / "tiny.wav"
        _make_silent_wav(audio, 0.1)

        result = _invoke(
            qa_audio_completeness,
            scene_id="scene_1",
            audio_path=str(audio),
        )
        assert result["verdict"] == VERDICT_FAIL
        assert "too short" in result["error"]


# ---------------------------------------------------------------------------
# Module-level surface
# ---------------------------------------------------------------------------


class TestModuleSurface:
    def test_exports_four_gates(self) -> None:
        assert "qa_duration_align" in qa_gates.__all__
        assert "qa_stills_judge" in qa_gates.__all__
        assert "qa_video_artifact_probe" in qa_gates.__all__
        assert "qa_audio_completeness" in qa_gates.__all__

    def test_all_gates_are_langchain_tools(self) -> None:
        # Sanity: each gate is decorated with @tool and exposes the
        # langchain ``invoke`` method the orchestrator uses.
        assert hasattr(qa_duration_align, "invoke")
        assert hasattr(qa_stills_judge, "invoke")
        assert hasattr(qa_video_artifact_probe, "invoke")
        assert hasattr(qa_audio_completeness, "invoke")

    def test_default_tolerance_matches_post_mortem(self) -> None:
        # User instructed in slice 9j post-mortem: hard fail at 0.5 s.
        assert DEFAULT_DURATION_TOLERANCE_S == 0.5
