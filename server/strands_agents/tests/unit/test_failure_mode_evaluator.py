"""Unit tests for :class:`FailureModeEvaluator`.

Exercises the evaluator's grading logic against synthetic
:class:`EvaluationData` — no pixels, no ffmpeg, just the deterministic
threshold comparisons.
"""

from __future__ import annotations

from typing import Any

from strands_evals.types.evaluation import EvaluationData

from strands_agents.evals.evaluators.failure_mode import FailureModeEvaluator


def _case(
    *,
    mode: str,
    signals: dict[str, Any],
    fixture_id: str = "test_fixture",
) -> EvaluationData[dict[str, Any], dict[str, Any]]:
    """Build an :class:`EvaluationData` wired the way the real task does."""
    return EvaluationData[dict[str, Any], dict[str, Any]](
        input={"id": fixture_id},
        actual_output={"signals": signals, "local_path": "/fake.mp4"},
        expected_output={"failure_mode": mode},
        metadata={
            "fixture_id": fixture_id,
            "expected_failure_mode": mode,
        },
    )


class TestFrozenMode:
    def test_passes_when_diff_below_threshold(self) -> None:
        evaluator = FailureModeEvaluator()
        data = _case(mode="frozen", signals={"max_interframe_diff": 0.1})
        outs = evaluator.evaluate(data)
        assert [o.test_pass for o in outs] == [True]
        assert outs[0].label == "failure_mode.frozen"

    def test_fails_when_diff_at_threshold(self) -> None:
        evaluator = FailureModeEvaluator(frozen_max_diff=0.5)
        data = _case(mode="frozen", signals={"max_interframe_diff": 0.5})
        outs = evaluator.evaluate(data)
        assert outs[0].test_pass is False

    def test_fails_when_diff_high(self) -> None:
        evaluator = FailureModeEvaluator()
        data = _case(mode="frozen", signals={"max_interframe_diff": 10.0})
        outs = evaluator.evaluate(data)
        assert outs[0].test_pass is False


class TestBlackMode:
    def test_passes_when_ratio_at_threshold(self) -> None:
        evaluator = FailureModeEvaluator(black_ratio_threshold=0.9)
        data = _case(mode="black", signals={"black_ratio": 0.9})
        outs = evaluator.evaluate(data)
        assert outs[0].test_pass is True

    def test_fails_when_ratio_low(self) -> None:
        evaluator = FailureModeEvaluator()
        data = _case(mode="black", signals={"black_ratio": 0.1})
        outs = evaluator.evaluate(data)
        assert outs[0].test_pass is False


class TestWhiteMode:
    def test_passes_when_ratio_high(self) -> None:
        evaluator = FailureModeEvaluator()
        data = _case(mode="white", signals={"white_ratio": 1.0})
        outs = evaluator.evaluate(data)
        assert outs[0].test_pass is True

    def test_fails_when_ratio_low(self) -> None:
        evaluator = FailureModeEvaluator()
        data = _case(mode="white", signals={"white_ratio": 0.0})
        outs = evaluator.evaluate(data)
        assert outs[0].test_pass is False


class TestCleanMode:
    def test_passes_when_both_ratios_low(self) -> None:
        evaluator = FailureModeEvaluator()
        data = _case(
            mode="clean",
            signals={"black_ratio": 0.0, "white_ratio": 0.0},
        )
        outs = evaluator.evaluate(data)
        assert [o.test_pass for o in outs] == [True, True]
        assert {o.label for o in outs} == {
            "failure_mode.clean.not_black",
            "failure_mode.clean.not_white",
        }

    def test_fails_when_mostly_black(self) -> None:
        evaluator = FailureModeEvaluator()
        data = _case(
            mode="clean",
            signals={"black_ratio": 1.0, "white_ratio": 0.0},
        )
        outs = evaluator.evaluate(data)
        passes = {o.label: o.test_pass for o in outs}
        assert passes["failure_mode.clean.not_black"] is False
        assert passes["failure_mode.clean.not_white"] is True

    def test_fails_when_mostly_white(self) -> None:
        evaluator = FailureModeEvaluator()
        data = _case(
            mode="clean",
            signals={"black_ratio": 0.0, "white_ratio": 1.0},
        )
        outs = evaluator.evaluate(data)
        passes = {o.label: o.test_pass for o in outs}
        assert passes["failure_mode.clean.not_black"] is True
        assert passes["failure_mode.clean.not_white"] is False


class TestSetupErrors:
    def test_missing_signals_fails_setup_clause(self) -> None:
        evaluator = FailureModeEvaluator()
        data = EvaluationData[dict[str, Any], dict[str, Any]](
            input={},
            actual_output={},
            expected_output=None,
            metadata={"expected_failure_mode": "frozen"},
        )
        outs = evaluator.evaluate(data)
        assert len(outs) == 1
        assert outs[0].test_pass is False
        assert outs[0].label == "failure_mode.setup"

    def test_invalid_mode_fails_setup_clause(self) -> None:
        evaluator = FailureModeEvaluator()
        data = _case(mode="no-such-mode", signals={"black_ratio": 0.0})
        outs = evaluator.evaluate(data)
        assert len(outs) == 1
        assert outs[0].test_pass is False
        assert outs[0].label == "failure_mode.setup"

    def test_missing_mode_fails_setup_clause(self) -> None:
        evaluator = FailureModeEvaluator()
        data = EvaluationData[dict[str, Any], dict[str, Any]](
            input={},
            actual_output={"signals": {"black_ratio": 0.0}},
            expected_output=None,
            metadata={},
        )
        outs = evaluator.evaluate(data)
        assert len(outs) == 1
        assert outs[0].test_pass is False
