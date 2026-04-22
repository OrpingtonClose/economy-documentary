"""Unit tests for :class:`AVDesyncEvaluator` grading logic.

The evaluator is pure — it reads signals off
``actual_output["signals"]`` and grades them against the expected
desync mode from case metadata. Every test here constructs an
:class:`EvaluationData` by hand (no task invocation, no ffmpeg
decode) and asserts the grading clauses come out correctly.
"""

from __future__ import annotations

from typing import Any

from strands_evals.types.evaluation import EvaluationData

from strands_agents.evals.evaluators.av_desync import (
    DEFAULT_AUDIO_MISSING_RMS_FLOOR,
    DEFAULT_DESYNC_MIN_SEC,
    DEFAULT_SYNC_TOLERANCE_SEC,
    AVDesyncEvaluator,
)


def _make_case(
    *,
    mode: str,
    audio_onset_sec: float | None = 0.0,
    video_content_onset_sec: float | None = 0.0,
    desync_sec: float | None = 0.0,
    audio_rms: float = 0.05,
    fixture_id: str = "fx",
) -> EvaluationData[dict[str, Any], dict[str, Any]]:
    """Construct an :class:`EvaluationData` with the declared signals."""
    return EvaluationData[dict[str, Any], dict[str, Any]](
        input={"id": fixture_id},
        actual_output={
            "signals": {
                "audio_onset_sec": audio_onset_sec,
                "video_content_onset_sec": video_content_onset_sec,
                "desync_sec": desync_sec,
                "audio_rms": audio_rms,
            }
        },
        expected_output={"desync_mode": mode},
        metadata={"expected_desync_mode": mode, "fixture_id": fixture_id},
    )


class TestSyncedMode:
    def test_clean_sync_emits_three_passing_clauses(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="synced",
                audio_onset_sec=0.04,
                video_content_onset_sec=0.0,
                desync_sec=0.04,
            )
        )
        assert len(outputs) == 3
        labels = {o.label for o in outputs}
        assert labels == {
            "av_desync.synced.audio_present",
            "av_desync.synced.video_present",
            "av_desync.synced.within_tolerance",
        }
        assert all(o.test_pass for o in outputs)

    def test_missing_audio_fails_audio_present_clause(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="synced",
                audio_onset_sec=None,
                video_content_onset_sec=0.0,
                desync_sec=None,
            )
        )
        by_label = {o.label: o for o in outputs}
        assert by_label["av_desync.synced.audio_present"].test_pass is False
        assert by_label["av_desync.synced.video_present"].test_pass is True
        assert by_label["av_desync.synced.within_tolerance"].test_pass is False

    def test_missing_video_fails_video_present_clause(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="synced",
                audio_onset_sec=0.04,
                video_content_onset_sec=None,
                desync_sec=None,
            )
        )
        by_label = {o.label: o for o in outputs}
        assert by_label["av_desync.synced.audio_present"].test_pass is True
        assert by_label["av_desync.synced.video_present"].test_pass is False
        assert by_label["av_desync.synced.within_tolerance"].test_pass is False

    def test_desync_above_tolerance_fails_within_tolerance_clause(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="synced",
                audio_onset_sec=0.5,
                video_content_onset_sec=0.0,
                desync_sec=0.5,
            )
        )
        by_label = {o.label: o for o in outputs}
        assert by_label["av_desync.synced.within_tolerance"].test_pass is False

    def test_tolerance_boundary_inclusive(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="synced",
                audio_onset_sec=DEFAULT_SYNC_TOLERANCE_SEC,
                video_content_onset_sec=0.0,
                desync_sec=DEFAULT_SYNC_TOLERANCE_SEC,
            )
        )
        by_label = {o.label: o for o in outputs}
        assert by_label["av_desync.synced.within_tolerance"].test_pass is True

    def test_negative_desync_inside_tolerance_passes(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="synced",
                audio_onset_sec=0.0,
                video_content_onset_sec=0.05,
                desync_sec=-0.05,
            )
        )
        by_label = {o.label: o for o in outputs}
        assert by_label["av_desync.synced.within_tolerance"].test_pass is True


class TestAudioAheadMode:
    def test_passes_when_audio_well_ahead(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="audio_ahead", desync_sec=-0.5)
        )
        assert len(outputs) == 1
        assert outputs[0].test_pass is True
        assert outputs[0].label == "av_desync.audio_ahead"

    def test_fails_when_desync_below_min(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="audio_ahead", desync_sec=-0.1)
        )
        assert outputs[0].test_pass is False

    def test_fails_on_wrong_sign(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="audio_ahead", desync_sec=+0.5)
        )
        assert outputs[0].test_pass is False

    def test_fails_when_desync_is_none(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="audio_ahead", desync_sec=None)
        )
        assert outputs[0].test_pass is False
        assert "None" in outputs[0].reason

    def test_boundary_inclusive(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="audio_ahead",
                desync_sec=-DEFAULT_DESYNC_MIN_SEC,
            )
        )
        assert outputs[0].test_pass is True


class TestAudioBehindMode:
    def test_passes_when_audio_well_behind(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="audio_behind", desync_sec=+0.6)
        )
        assert len(outputs) == 1
        assert outputs[0].test_pass is True
        assert outputs[0].label == "av_desync.audio_behind"

    def test_fails_when_desync_below_min(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="audio_behind", desync_sec=+0.1)
        )
        assert outputs[0].test_pass is False

    def test_fails_on_wrong_sign(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="audio_behind", desync_sec=-0.5)
        )
        assert outputs[0].test_pass is False

    def test_fails_when_desync_is_none(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="audio_behind", desync_sec=None)
        )
        assert outputs[0].test_pass is False

    def test_boundary_inclusive(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="audio_behind",
                desync_sec=DEFAULT_DESYNC_MIN_SEC,
            )
        )
        assert outputs[0].test_pass is True


class TestAudioMissingMode:
    def test_passes_when_onset_none_and_rms_below_floor(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="audio_missing",
                audio_onset_sec=None,
                audio_rms=0.0,
            )
        )
        assert len(outputs) == 1
        assert outputs[0].test_pass is True
        assert outputs[0].label == "av_desync.audio_missing"

    def test_fails_when_onset_present(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="audio_missing",
                audio_onset_sec=0.05,
                audio_rms=0.0,
            )
        )
        assert outputs[0].test_pass is False

    def test_fails_when_rms_above_floor(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="audio_missing",
                audio_onset_sec=None,
                audio_rms=0.01,
            )
        )
        assert outputs[0].test_pass is False

    def test_boundary_rms_inclusive(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="audio_missing",
                audio_onset_sec=None,
                audio_rms=DEFAULT_AUDIO_MISSING_RMS_FLOOR,
            )
        )
        assert outputs[0].test_pass is True


class TestVideoMissingMode:
    def test_passes_when_video_onset_none(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="video_missing", video_content_onset_sec=None
            )
        )
        assert len(outputs) == 1
        assert outputs[0].test_pass is True
        assert outputs[0].label == "av_desync.video_missing"

    def test_fails_when_video_onset_present(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="video_missing", video_content_onset_sec=0.0
            )
        )
        assert outputs[0].test_pass is False


class TestSetupFailures:
    def test_unknown_mode_fails_setup(self) -> None:
        evaluator = AVDesyncEvaluator()
        outputs = evaluator.evaluate(_make_case(mode="nonsense"))
        assert len(outputs) == 1
        assert outputs[0].test_pass is False
        assert outputs[0].label == "av_desync.setup"
        assert "expected_desync_mode" in outputs[0].reason

    def test_missing_signals_fails_setup(self) -> None:
        evaluator = AVDesyncEvaluator()
        case = EvaluationData[dict[str, Any], dict[str, Any]](
            input={"id": "fx"},
            actual_output={},
            expected_output={"desync_mode": "synced"},
            metadata={"expected_desync_mode": "synced", "fixture_id": "fx"},
        )
        outputs = evaluator.evaluate(case)
        assert outputs[0].test_pass is False
        assert outputs[0].label == "av_desync.setup"

    def test_missing_metadata_fails_setup(self) -> None:
        evaluator = AVDesyncEvaluator()
        case = EvaluationData[dict[str, Any], dict[str, Any]](
            input={"id": "fx"},
            actual_output={"signals": {"audio_onset_sec": 0.0}},
            expected_output={"desync_mode": "synced"},
            metadata={},
        )
        outputs = evaluator.evaluate(case)
        assert outputs[0].test_pass is False
        assert outputs[0].label == "av_desync.setup"


class TestCustomThresholds:
    def test_custom_sync_tolerance_tightens_gate(self) -> None:
        evaluator = AVDesyncEvaluator(sync_tolerance_sec=0.05)
        outputs = evaluator.evaluate(
            _make_case(
                mode="synced",
                audio_onset_sec=0.1,
                video_content_onset_sec=0.0,
                desync_sec=0.1,
            )
        )
        by_label = {o.label: o for o in outputs}
        # default would pass (0.1 < 0.15), but custom fails (0.1 > 0.05)
        assert by_label["av_desync.synced.within_tolerance"].test_pass is False

    def test_custom_desync_min_loosens_gate(self) -> None:
        evaluator = AVDesyncEvaluator(desync_min_sec=0.1)
        outputs = evaluator.evaluate(
            _make_case(mode="audio_behind", desync_sec=+0.15)
        )
        # default would fail (0.15 < 0.3), but custom passes (0.15 > 0.1)
        assert outputs[0].test_pass is True

    def test_custom_audio_missing_rms_floor(self) -> None:
        evaluator = AVDesyncEvaluator(audio_missing_rms_floor=0.0001)
        outputs = evaluator.evaluate(
            _make_case(
                mode="audio_missing",
                audio_onset_sec=None,
                audio_rms=0.0005,
            )
        )
        # default rms_floor=0.001 would pass, custom 0.0001 fails
        assert outputs[0].test_pass is False
