"""Unit tests for :mod:`strands_agents.evals._runner`."""

from __future__ import annotations

from typing import Any

import pytest
from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from strands_agents.evals._runner import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_SKIP,
    run_experiment_as_main,
)


class _AlwaysPassEvaluator(Evaluator):
    """Deterministic evaluator that passes every case."""

    @property
    def name(self) -> str:
        return "always_pass"

    def evaluate(self, data: EvaluationData) -> list[EvaluationOutput]:
        return [
            EvaluationOutput(
                score=1.0,
                test_pass=True,
                reason="always green",
            )
        ]


class _AlwaysFailEvaluator(Evaluator):
    """Deterministic evaluator that fails every case."""

    @property
    def name(self) -> str:
        return "always_fail"

    def evaluate(self, data: EvaluationData) -> list[EvaluationOutput]:
        return [
            EvaluationOutput(
                score=0.0,
                test_pass=False,
                reason="always red",
            )
        ]


def _build_passing_experiment() -> Experiment:
    return Experiment(
        cases=[Case(name="c1", input={}, expected_output={})],
        evaluators=[_AlwaysPassEvaluator()],
    )


def _build_failing_experiment() -> Experiment:
    return Experiment(
        cases=[Case(name="c1", input={}, expected_output={})],
        evaluators=[_AlwaysFailEvaluator()],
    )


def _build_empty_experiment() -> Experiment:
    return Experiment(
        cases=[],
        evaluators=[_AlwaysPassEvaluator()],
    )


def _task(case: Case) -> dict[str, Any]:
    return {"output": {"echo": case.name}}


def test_run_experiment_as_main_returns_exit_pass_when_all_green(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = run_experiment_as_main(_build_passing_experiment, _task)
    out = capsys.readouterr().out
    assert rc == EXIT_PASS
    assert "passed=1/1" in out
    assert "summary:" in out


def test_run_experiment_as_main_returns_exit_fail_on_any_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = run_experiment_as_main(_build_failing_experiment, _task)
    out = capsys.readouterr().out
    assert rc == EXIT_FAIL
    assert "FAIL c1" in out


def test_run_experiment_as_main_skips_on_missing_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("_RUNNER_UNIT_TEST_KEY_DOES_NOT_EXIST", raising=False)
    rc = run_experiment_as_main(
        _build_passing_experiment,
        _task,
        required_env=("_RUNNER_UNIT_TEST_KEY_DOES_NOT_EXIST",),
    )
    err = capsys.readouterr().err
    assert rc == EXIT_SKIP
    assert "SKIP" in err
    assert "_RUNNER_UNIT_TEST_KEY_DOES_NOT_EXIST" in err


def test_run_experiment_as_main_skips_on_empty_cases(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = run_experiment_as_main(_build_empty_experiment, _task)
    err = capsys.readouterr().err
    assert rc == EXIT_SKIP
    assert "no cases" in err


def test_run_experiment_as_main_skips_when_factory_raises_value_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _raising_factory() -> Experiment:
        raise ValueError("no closed pairs for this media kind")

    rc = run_experiment_as_main(_raising_factory, _task)
    err = capsys.readouterr().err
    assert rc == EXIT_SKIP
    assert "no closed pairs" in err


def test_run_experiment_as_main_honours_env_when_present(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("_RUNNER_UNIT_TEST_KEY_PRESENT", "x")
    rc = run_experiment_as_main(
        _build_passing_experiment,
        _task,
        required_env=("_RUNNER_UNIT_TEST_KEY_PRESENT",),
    )
    assert rc == EXIT_PASS


def test_run_experiment_as_main_honours_expect_pass_false_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A case flagged ``expect_pass=False`` passes the gate when it fails."""

    def _build_with_negative_case() -> Experiment:
        return Experiment(
            cases=[
                Case(
                    name="negative",
                    input={},
                    expected_output={},
                    metadata={"expect_pass": False},
                )
            ],
            evaluators=[_AlwaysFailEvaluator()],
        )

    rc = run_experiment_as_main(_build_with_negative_case, _task)
    assert rc == EXIT_PASS


def test_run_experiment_as_main_fails_when_negative_case_passes_unexpectedly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A case flagged ``expect_pass=False`` fails the gate if it passes."""

    def _build_with_negative_case() -> Experiment:
        return Experiment(
            cases=[
                Case(
                    name="negative",
                    input={},
                    expected_output={},
                    metadata={"expect_pass": False},
                )
            ],
            evaluators=[_AlwaysPassEvaluator()],
        )

    rc = run_experiment_as_main(_build_with_negative_case, _task)
    assert rc == EXIT_FAIL
