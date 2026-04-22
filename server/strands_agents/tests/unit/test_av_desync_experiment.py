"""Unit tests for the A/V desync Experiment factory and cases.

Pins the factory's purity (no subprocess, no fixture I/O), the
case-building logic (fixture ids mapped to expected modes), and
the runner-level shape of the task output.
"""

from __future__ import annotations

from typing import Any

import pytest
from strands_evals.case import Case
from strands_evals.experiment import Experiment

from strands_agents.evals.evaluators import AVDesyncEvaluator
from strands_agents.evals.experiments.av_desync import (
    av_desync_task,
    build_av_desync_experiment,
    build_cases,
)


class TestBuildCases:
    def test_builds_cases_from_manifest(self) -> None:
        cases = build_cases()
        assert len(cases) > 0
        valid_modes = {
            "synced",
            "audio_ahead",
            "audio_behind",
            "audio_missing",
            "video_missing",
        }
        for case in cases:
            metadata = case.metadata or {}
            assert metadata.get("expected_desync_mode") in valid_modes

    def test_every_declared_fixture_mapped(self) -> None:
        cases = build_cases()
        ids = {(c.metadata or {}).get("fixture_id") for c in cases}
        # All five AV fixtures should be present in the corpus.
        assert "av_synced_narration" in ids
        assert "av_audio_ahead" in ids
        assert "av_audio_behind" in ids
        assert "av_audio_missing" in ids
        assert "av_video_missing" in ids

    def test_case_names_include_mode_and_id(self) -> None:
        names = {case.name for case in build_cases()}
        assert any("synced.av_synced_narration" in n for n in names)
        assert any("audio_ahead.av_audio_ahead" in n for n in names)
        assert any("audio_behind.av_audio_behind" in n for n in names)
        assert any("audio_missing.av_audio_missing" in n for n in names)
        assert any("video_missing.av_video_missing" in n for n in names)


class TestBuildExperiment:
    def test_factory_returns_experiment_with_single_evaluator(self) -> None:
        experiment = build_av_desync_experiment()
        assert isinstance(experiment, Experiment)
        assert len(experiment.evaluators) == 1
        assert isinstance(experiment.evaluators[0], AVDesyncEvaluator)

    def test_factory_has_cases(self) -> None:
        experiment = build_av_desync_experiment()
        assert len(experiment.cases) > 0

    def test_every_case_carries_desync_mode_metadata(self) -> None:
        experiment = build_av_desync_experiment()
        for case in experiment.cases:
            assert (case.metadata or {}).get("expected_desync_mode") is not None


class TestTask:
    def test_task_returns_signals_envelope(self) -> None:
        cases = build_cases()
        case = next(
            c
            for c in cases
            if (c.metadata or {}).get("fixture_id") == "av_synced_narration"
        )
        result = av_desync_task(case)
        assert "output" in result
        output = result["output"]
        assert "local_path" in output
        assert "signals" in output
        signals = output["signals"]
        for key in (
            "path",
            "duration_sec",
            "has_video_stream",
            "has_audio_stream",
            "audio_onset_sec",
            "video_content_onset_sec",
            "desync_sec",
            "audio_rms",
        ):
            assert key in signals

    def test_task_on_synced_fixture_desync_within_tolerance(self) -> None:
        cases = build_cases()
        case = next(
            c
            for c in cases
            if (c.metadata or {}).get("fixture_id") == "av_synced_narration"
        )
        result = av_desync_task(case)
        signals = result["output"]["signals"]
        assert signals["audio_onset_sec"] is not None
        assert signals["video_content_onset_sec"] is not None
        assert abs(signals["desync_sec"]) <= 0.15

    def test_task_on_audio_ahead_has_negative_desync(self) -> None:
        cases = build_cases()
        case = next(
            c
            for c in cases
            if (c.metadata or {}).get("fixture_id") == "av_audio_ahead"
        )
        result = av_desync_task(case)
        signals = result["output"]["signals"]
        assert signals["desync_sec"] is not None
        assert signals["desync_sec"] <= -0.3

    def test_task_on_audio_behind_has_positive_desync(self) -> None:
        cases = build_cases()
        case = next(
            c
            for c in cases
            if (c.metadata or {}).get("fixture_id") == "av_audio_behind"
        )
        result = av_desync_task(case)
        signals = result["output"]["signals"]
        assert signals["desync_sec"] is not None
        assert signals["desync_sec"] >= 0.3

    def test_task_on_audio_missing_has_no_audio_onset(self) -> None:
        cases = build_cases()
        case = next(
            c
            for c in cases
            if (c.metadata or {}).get("fixture_id") == "av_audio_missing"
        )
        result = av_desync_task(case)
        signals = result["output"]["signals"]
        assert signals["audio_onset_sec"] is None
        assert signals["desync_sec"] is None

    def test_task_on_video_missing_has_no_video_onset(self) -> None:
        cases = build_cases()
        case = next(
            c
            for c in cases
            if (c.metadata or {}).get("fixture_id") == "av_video_missing"
        )
        result = av_desync_task(case)
        signals = result["output"]["signals"]
        assert signals["video_content_onset_sec"] is None
        assert signals["desync_sec"] is None


class TestExperimentEndToEnd:
    def test_full_run_exits_green(self) -> None:
        experiment = build_av_desync_experiment()
        reports = experiment.run_evaluations(task=av_desync_task)
        assert len(reports) == 1
        report = reports[0]
        assert report.overall_score == pytest.approx(1.0)
        assert all(report.test_passes)
