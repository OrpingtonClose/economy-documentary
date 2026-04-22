"""Unit tests for :class:`AudioFailureModeEvaluator` grading logic.

The evaluator is pure — it reads signals off
``actual_output["signals"]`` and grades them against the expected
failure mode from case metadata. Every test here constructs an
:class:`EvaluationData` by hand (no task invocation, no fixture
I/O) and asserts the grading clauses come out correctly.
"""

from __future__ import annotations

from typing import Any

from strands_evals.types.evaluation import EvaluationData

from strands_agents.evals.evaluators.audio_failure_mode import (
    DEFAULT_CLIPPING_RATIO_THRESHOLD,
    DEFAULT_NOISE_FLATNESS_THRESHOLD,
    DEFAULT_SILENCE_RMS_THRESHOLD,
    AudioFailureModeEvaluator,
)


def _make_case(
    *,
    mode: str,
    rms: float = 0.08,
    clipping_ratio: float = 0.0,
    spectral_flatness: float = 0.3,
    fixture_id: str = "fx",
) -> EvaluationData[dict[str, Any], dict[str, Any]]:
    """Construct an :class:`EvaluationData` with the declared signals."""
    return EvaluationData[dict[str, Any], dict[str, Any]](
        input={"id": fixture_id},
        actual_output={
            "signals": {
                "rms": rms,
                "clipping_ratio": clipping_ratio,
                "spectral_flatness": spectral_flatness,
            }
        },
        expected_output={"failure_mode": mode},
        metadata={"expected_failure_mode": mode, "fixture_id": fixture_id},
    )


class TestSilenceMode:
    def test_silence_passes_when_rms_below_threshold(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(_make_case(mode="silence", rms=0.0))
        assert len(outputs) == 1
        assert outputs[0].test_pass is True
        assert outputs[0].label == "audio_failure_mode.silence"

    def test_silence_fails_when_rms_above_threshold(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(_make_case(mode="silence", rms=0.05))
        assert len(outputs) == 1
        assert outputs[0].test_pass is False

    def test_silence_boundary_inclusive(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="silence", rms=DEFAULT_SILENCE_RMS_THRESHOLD)
        )
        assert outputs[0].test_pass is True


class TestClippingMode:
    def test_clipping_passes_when_ratio_above_threshold(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="clipping", clipping_ratio=0.05)
        )
        assert len(outputs) == 1
        assert outputs[0].test_pass is True
        assert outputs[0].label == "audio_failure_mode.clipping"

    def test_clipping_fails_when_ratio_below_threshold(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="clipping", clipping_ratio=0.0)
        )
        assert outputs[0].test_pass is False

    def test_clipping_boundary_inclusive(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="clipping",
                clipping_ratio=DEFAULT_CLIPPING_RATIO_THRESHOLD,
            )
        )
        assert outputs[0].test_pass is True


class TestNoiseMode:
    def test_noise_passes_when_flatness_above_threshold(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="noise", spectral_flatness=0.85)
        )
        assert len(outputs) == 1
        assert outputs[0].test_pass is True
        assert outputs[0].label == "audio_failure_mode.noise"

    def test_noise_fails_when_flatness_below_threshold(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(
            _make_case(mode="noise", spectral_flatness=0.3)
        )
        assert outputs[0].test_pass is False

    def test_noise_boundary_inclusive(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="noise",
                spectral_flatness=DEFAULT_NOISE_FLATNESS_THRESHOLD,
            )
        )
        assert outputs[0].test_pass is True


class TestCleanMode:
    def test_clean_emits_three_clauses(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="clean",
                rms=0.08,
                clipping_ratio=0.0,
                spectral_flatness=0.3,
            )
        )
        assert len(outputs) == 3
        labels = {o.label for o in outputs}
        assert labels == {
            "audio_failure_mode.clean.not_silent",
            "audio_failure_mode.clean.not_clipping",
            "audio_failure_mode.clean.not_noise",
        }
        assert all(o.test_pass for o in outputs)

    def test_clean_fails_when_silent(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="clean",
                rms=0.0,
                clipping_ratio=0.0,
                spectral_flatness=0.3,
            )
        )
        by_label = {o.label: o for o in outputs}
        assert by_label["audio_failure_mode.clean.not_silent"].test_pass is False
        assert by_label["audio_failure_mode.clean.not_clipping"].test_pass is True
        assert by_label["audio_failure_mode.clean.not_noise"].test_pass is True

    def test_clean_fails_when_clipping(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="clean",
                rms=0.08,
                clipping_ratio=0.1,
                spectral_flatness=0.3,
            )
        )
        by_label = {o.label: o for o in outputs}
        assert by_label["audio_failure_mode.clean.not_silent"].test_pass is True
        assert by_label["audio_failure_mode.clean.not_clipping"].test_pass is False
        assert by_label["audio_failure_mode.clean.not_noise"].test_pass is True

    def test_clean_fails_when_noise(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        outputs = evaluator.evaluate(
            _make_case(
                mode="clean",
                rms=0.08,
                clipping_ratio=0.0,
                spectral_flatness=0.9,
            )
        )
        by_label = {o.label: o for o in outputs}
        assert by_label["audio_failure_mode.clean.not_silent"].test_pass is True
        assert by_label["audio_failure_mode.clean.not_clipping"].test_pass is True
        assert by_label["audio_failure_mode.clean.not_noise"].test_pass is False


class TestSetupFailures:
    def test_unknown_mode_fails(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        case = EvaluationData[dict[str, Any], dict[str, Any]](
            input={"id": "x"},
            actual_output={"signals": {"rms": 0.1}},
            expected_output={},
            metadata={"expected_failure_mode": "invalid"},
        )
        outputs = evaluator.evaluate(case)
        assert len(outputs) == 1
        assert outputs[0].test_pass is False
        assert outputs[0].label == "audio_failure_mode.setup"

    def test_missing_signals_fails(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        case = EvaluationData[dict[str, Any], dict[str, Any]](
            input={"id": "x"},
            actual_output={},
            expected_output={},
            metadata={"expected_failure_mode": "clean"},
        )
        outputs = evaluator.evaluate(case)
        assert len(outputs) == 1
        assert outputs[0].test_pass is False
        assert outputs[0].label == "audio_failure_mode.setup"

    def test_no_metadata_is_treated_as_missing_mode(self) -> None:
        evaluator = AudioFailureModeEvaluator()
        case = EvaluationData[dict[str, Any], dict[str, Any]](
            input={"id": "x"},
            actual_output={"signals": {"rms": 0.1}},
            expected_output={},
            metadata=None,
        )
        outputs = evaluator.evaluate(case)
        assert len(outputs) == 1
        assert outputs[0].test_pass is False
        assert outputs[0].label == "audio_failure_mode.setup"


class TestCustomThresholds:
    def test_custom_silence_threshold(self) -> None:
        # RMS 0.05 should pass silence if threshold is widened to 0.1.
        evaluator = AudioFailureModeEvaluator(silence_rms_threshold=0.1)
        outputs = evaluator.evaluate(_make_case(mode="silence", rms=0.05))
        assert outputs[0].test_pass is True

    def test_custom_clipping_threshold(self) -> None:
        # Clipping ratio 0.005 should pass clipping if threshold is 0.001.
        evaluator = AudioFailureModeEvaluator(clipping_ratio_threshold=0.001)
        outputs = evaluator.evaluate(
            _make_case(mode="clipping", clipping_ratio=0.005)
        )
        assert outputs[0].test_pass is True

    def test_custom_noise_threshold(self) -> None:
        # Flatness 0.5 should pass noise if threshold is lowered.
        evaluator = AudioFailureModeEvaluator(noise_flatness_threshold=0.4)
        outputs = evaluator.evaluate(
            _make_case(mode="noise", spectral_flatness=0.5)
        )
        assert outputs[0].test_pass is True
