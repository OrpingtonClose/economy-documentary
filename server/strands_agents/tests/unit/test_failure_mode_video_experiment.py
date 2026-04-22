"""Smoke tests for the failure-mode video Experiment factory.

Confirms the experiment enrolls the expected fixtures from the
committed manifest and that its cases carry the right metadata. The
heavy lifting (actual pixel decode + grading) is covered end-to-end
by invoking the ``python -m`` entrypoint in CI; this module only
checks the wiring.
"""

from __future__ import annotations

from strands_agents.evals.experiments.failure_mode_video import (
    _CLEAN_CONTROL_FIXTURES,
    _FAILURE_MODE_FIXTURES,
    build_cases,
    build_failure_mode_video_experiment,
)


def test_build_cases_enrolls_every_failure_fixture() -> None:
    cases = build_cases()
    names = {c.name for c in cases}
    for fixture_id, mode in _FAILURE_MODE_FIXTURES.items():
        expected = f"failure_mode.{mode}.{fixture_id}"
        assert expected in names, f"missing reject case for {fixture_id}"


def test_build_cases_enrolls_every_clean_control() -> None:
    cases = build_cases()
    names = {c.name for c in cases}
    for fixture_id in _CLEAN_CONTROL_FIXTURES:
        expected = f"failure_mode.clean.{fixture_id}"
        assert expected in names, f"missing clean case for {fixture_id}"


def test_clean_controls_exclude_semantic_black_white() -> None:
    # These judge-facing fixtures are semantically black/white and
    # would legitimately trip the detectors — they must not be used
    # as clean positive controls, or the QA gate becomes meaningless.
    assert "video_solid_black_color" not in _CLEAN_CONTROL_FIXTURES
    assert "video_solid_white_not_black" not in _CLEAN_CONTROL_FIXTURES


def test_each_case_metadata_carries_expected_failure_mode() -> None:
    for case in build_cases():
        assert case.metadata is not None
        mode = case.metadata.get("expected_failure_mode")
        assert mode in {"frozen", "black", "white", "clean"}


def test_experiment_has_single_failure_mode_evaluator() -> None:
    experiment = build_failure_mode_video_experiment()
    evaluators = list(experiment.evaluators)
    assert len(evaluators) == 1
    assert type(evaluators[0]).__name__ == "FailureModeEvaluator"
