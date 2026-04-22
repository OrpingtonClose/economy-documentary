"""Unit tests for the audio failure-mode Experiment factory and cases.

Pins the factory's purity (no subprocess, no fixture I/O), the
case-building logic (fixture ids mapped to expected modes), and
the runner-level shape of the task output.
"""

from __future__ import annotations

from typing import Any

import pytest
from strands_evals.case import Case
from strands_evals.experiment import Experiment

from strands_agents.evals.evaluators import AudioFailureModeEvaluator
from strands_agents.evals.experiments.audio_failure_mode import (
    audio_failure_mode_task,
    build_audio_failure_mode_experiment,
    build_cases,
)


class TestBuildCases:
    def test_builds_cases_from_manifest(self) -> None:
        cases = build_cases()
        assert len(cases) > 0
        # Every case carries a recognised failure mode.
        valid_modes = {"silence", "clipping", "noise", "clean"}
        for case in cases:
            metadata = case.metadata or {}
            assert metadata.get("expected_failure_mode") in valid_modes

    def test_includes_reject_fixtures(self) -> None:
        names = {case.name for case in build_cases()}
        assert any("silence.audio_silence" in n for n in names)
        assert any("noise.audio_noise_only" in n for n in names)
        assert any("clipping.audio_clipping" in n for n in names)

    def test_includes_clean_controls(self) -> None:
        names = {case.name for case in build_cases()}
        assert any("clean.audio_hello_narration" in n for n in names)
        assert any("clean.audio_english_narration" in n for n in names)


class TestBuildExperiment:
    def test_factory_returns_experiment_with_single_evaluator(self) -> None:
        experiment = build_audio_failure_mode_experiment()
        assert isinstance(experiment, Experiment)
        assert len(experiment.evaluators) == 1
        assert isinstance(experiment.evaluators[0], AudioFailureModeEvaluator)

    def test_factory_has_cases(self) -> None:
        experiment = build_audio_failure_mode_experiment()
        assert len(experiment.cases) > 0


class TestTask:
    def test_task_returns_signals_envelope(self) -> None:
        cases = build_cases()
        case = next(
            c
            for c in cases
            if (c.metadata or {}).get("fixture_id") == "audio_silence"
        )
        result = audio_failure_mode_task(case)
        assert "output" in result
        output = result["output"]
        assert "local_path" in output
        assert "signals" in output
        signals = output["signals"]
        for key in (
            "path",
            "duration_sec",
            "sample_rate",
            "num_samples",
            "rms",
            "peak",
            "clipping_ratio",
            "spectral_flatness",
        ):
            assert key in signals

    def test_task_on_silence_fixture_has_zero_rms(self) -> None:
        cases = build_cases()
        case = next(
            c
            for c in cases
            if (c.metadata or {}).get("fixture_id") == "audio_silence"
        )
        result = audio_failure_mode_task(case)
        assert result["output"]["signals"]["rms"] == 0.0

    def test_task_on_clipping_fixture_has_peak_at_rail(self) -> None:
        cases = build_cases()
        case = next(
            c
            for c in cases
            if (c.metadata or {}).get("fixture_id") == "audio_clipping"
        )
        result = audio_failure_mode_task(case)
        signals = result["output"]["signals"]
        assert signals["peak"] >= 0.99
        assert signals["clipping_ratio"] > 0.0
