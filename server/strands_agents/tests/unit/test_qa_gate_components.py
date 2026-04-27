"""Unit tests for the three QA-gate ``/components`` workbench cards.

PR slice 9n surfaces :func:`strands_agents.qa_gates.qa_video_artifact_probe`,
:func:`strands_agents.qa_gates.qa_duration_align`, and
:func:`strands_agents.qa_gates.qa_stills_judge` as
``/components/qa_*`` cards. These tests assert each module follows the
same contract as the other infra experiments:

* ``*_cases()`` returns at least one :class:`Case` per branch the
  underlying gate can take.
* ``build_*_experiment()`` wires the module's evaluators.
* ``*_task(case)`` synthesises the case's fixture, dispatches the
  *real* gate via ``langchain_core.tools.BaseTool.invoke``, and
  returns an envelope whose ``verdict`` matches the expectation.
* The registry entry resolves to the module without a missing-import
  surface.
* Running the experiment scores every case as ``test_pass=True``
  under the module's evaluators — the same path
  :func:`strands_evals.experiment.Experiment.run_evaluations` uses
  for CI.

The gates' own deterministic logic is covered separately in
``test_qa_gates.py``; this file specifically pins the
``/components`` surfacing.
"""

from __future__ import annotations

import shutil

import pytest

from strands_agents.evals.experiments.qa_duration_align import (
    QA_DURATION_ALIGN_EVALUATOR_THRESHOLDS,
    DurationAlignDetailEvaluator,
    DurationAlignVerdictEvaluator,
    build_qa_duration_align_experiment,
    qa_duration_align_cases,
    qa_duration_align_task,
)
from strands_agents.evals.experiments.qa_stills_judge import (
    QA_STILLS_JUDGE_EVALUATOR_THRESHOLDS,
    StillsJudgeDetailEvaluator,
    StillsJudgeVerdictEvaluator,
    build_qa_stills_judge_experiment,
    qa_stills_judge_cases,
    qa_stills_judge_task,
)
from strands_agents.evals.experiments.qa_video_artifact_probe import (
    QA_VIDEO_ARTIFACT_PROBE_EVALUATOR_THRESHOLDS,
    ProbeDetailEvaluator,
    ProbeVerdictEvaluator,
    build_qa_video_artifact_probe_experiment,
    qa_video_artifact_probe_cases,
    qa_video_artifact_probe_task,
)
from strands_agents.playground import INFRA_COMPONENT_IDS, get_component


_FFMPEG_AVAILABLE = (
    shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
)
_REQUIRES_FFMPEG = pytest.mark.skipif(
    not _FFMPEG_AVAILABLE,
    reason="ffmpeg / ffprobe not on PATH (required for QA-gate fixtures)",
)


# ── Registry surfacing ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "component_id",
    ("qa_video_artifact_probe", "qa_duration_align", "qa_stills_judge"),
)
def test_registry_entry_resolves(component_id: str) -> None:
    assert component_id in INFRA_COMPONENT_IDS
    component = get_component(component_id)
    assert component is not None
    assert component.kind == "gate"
    assert component.row == 4
    # Lazy-import path used by the playground at startup.
    cases = component.cases()
    assert cases, f"{component_id} surfaces no cases via the registry"
    evaluators = component.evaluators()
    assert evaluators, f"{component_id} surfaces no evaluators"


# ── Probe ────────────────────────────────────────────────────────────


def test_probe_cases_factory_returns_cases() -> None:
    cases = qa_video_artifact_probe_cases()
    names = {c.name for c in cases}
    assert {
        "probe_motion_clip",
        "probe_still_clip",
        "probe_missing_file",
        "probe_truncated_artifact",
    }.issubset(names)


def test_probe_thresholds_dict_lists_both_evaluators() -> None:
    assert set(QA_VIDEO_ARTIFACT_PROBE_EVALUATOR_THRESHOLDS) == {
        "ProbeVerdictEvaluator",
        "ProbeDetailEvaluator",
    }
    for threshold, hard_gate in QA_VIDEO_ARTIFACT_PROBE_EVALUATOR_THRESHOLDS.values():
        assert threshold == 1.0
        assert hard_gate is True


def test_probe_experiment_wires_both_evaluators() -> None:
    exp = build_qa_video_artifact_probe_experiment()
    names = {type(e).__name__ for e in exp.evaluators}
    assert names == {"ProbeVerdictEvaluator", "ProbeDetailEvaluator"}


@_REQUIRES_FFMPEG
def test_probe_task_dispatches_real_gate_for_every_case() -> None:
    cases = qa_video_artifact_probe_cases()
    verdict_eval = ProbeVerdictEvaluator()
    detail_eval = ProbeDetailEvaluator()

    for case in cases:
        result = qa_video_artifact_probe_task(case)
        envelope = result["output"]
        assert envelope.get("tool") == "qa_video_artifact_probe"
        expected = (case.metadata or {}).get("expected_verdict")
        assert envelope.get("verdict") == expected, (
            f"{case.name}: expected {expected}, got "
            f"{envelope.get('verdict')!r} ({envelope})"
        )

        from strands_evals.types.evaluation import EvaluationData

        eval_case = EvaluationData(
            input=case.input,
            expected_output=case.expected_output,
            actual_output=envelope,
            metadata=case.metadata,
        )
        for ev in (verdict_eval, detail_eval):
            outputs = ev.evaluate(eval_case)
            assert outputs and all(o.test_pass for o in outputs), (
                f"{case.name} failed {type(ev).__name__}: {[o.reason for o in outputs]}"
            )


# ── Duration align ───────────────────────────────────────────────────


def test_duration_align_cases_factory_returns_cases() -> None:
    cases = qa_duration_align_cases()
    names = {c.name for c in cases}
    # The slice 9j frozen-frame regression case is mandatory.
    assert "frozen_frame_regression_fails" in names
    assert "aligned_pair_passes" in names


def test_duration_align_thresholds_dict_lists_both_evaluators() -> None:
    assert set(QA_DURATION_ALIGN_EVALUATOR_THRESHOLDS) == {
        "DurationAlignVerdictEvaluator",
        "DurationAlignDetailEvaluator",
    }


def test_duration_align_experiment_wires_both_evaluators() -> None:
    exp = build_qa_duration_align_experiment()
    names = {type(e).__name__ for e in exp.evaluators}
    assert names == {
        "DurationAlignVerdictEvaluator",
        "DurationAlignDetailEvaluator",
    }


@_REQUIRES_FFMPEG
def test_duration_align_task_dispatches_real_gate_for_every_case() -> None:
    cases = qa_duration_align_cases()
    verdict_eval = DurationAlignVerdictEvaluator()
    detail_eval = DurationAlignDetailEvaluator()
    from strands_evals.types.evaluation import EvaluationData

    for case in cases:
        result = qa_duration_align_task(case)
        envelope = result["output"]
        assert envelope.get("tool") == "qa_duration_align"
        expected = (case.metadata or {}).get("expected_verdict")
        assert envelope.get("verdict") == expected, (
            f"{case.name}: expected {expected}, got "
            f"{envelope.get('verdict')!r} ({envelope})"
        )

        eval_case = EvaluationData(
            input=case.input,
            expected_output=case.expected_output,
            actual_output=envelope,
            metadata=case.metadata,
        )
        for ev in (verdict_eval, detail_eval):
            outputs = ev.evaluate(eval_case)
            assert outputs and all(o.test_pass for o in outputs), (
                f"{case.name} failed {type(ev).__name__}: {[o.reason for o in outputs]}"
            )


@_REQUIRES_FFMPEG
def test_duration_align_catches_slice_9j_regression() -> None:
    """Replay the exact slice 9j bug: 13 s narration + 3.7 s LTX-2.3 clip."""
    cases = {c.name: c for c in qa_duration_align_cases()}
    case = cases["frozen_frame_regression_fails"]
    result = qa_duration_align_task(case)
    envelope = result["output"]
    assert envelope["verdict"] == "fail"
    assert envelope["delta_s"] > 5.0, envelope
    assert envelope["delta_s"] > envelope["tolerance_s"], envelope


# ── Stills judge ─────────────────────────────────────────────────────


def test_stills_judge_cases_factory_returns_cases() -> None:
    cases = qa_stills_judge_cases()
    names = {c.name for c in cases}
    # The frozen-clip and motion-clip branches are mandatory; both
    # must land in the case list so the orchestrator-side gate can
    # be exercised against real ffmpeg-synthesised motion *and* a
    # solid-color still without a network round trip.
    assert {
        "frozen_grey_fails",
        "mandelbrot_motion_passes",
        "video_missing_fails",
    }.issubset(names)


def test_stills_judge_thresholds_dict_lists_both_evaluators() -> None:
    assert set(QA_STILLS_JUDGE_EVALUATOR_THRESHOLDS) == {
        "StillsJudgeVerdictEvaluator",
        "StillsJudgeDetailEvaluator",
    }


def test_stills_judge_experiment_wires_both_evaluators() -> None:
    exp = build_qa_stills_judge_experiment()
    names = {type(e).__name__ for e in exp.evaluators}
    assert names == {
        "StillsJudgeVerdictEvaluator",
        "StillsJudgeDetailEvaluator",
    }


@_REQUIRES_FFMPEG
def test_stills_judge_task_dispatches_real_gate_for_every_case() -> None:
    cases = qa_stills_judge_cases()
    verdict_eval = StillsJudgeVerdictEvaluator()
    detail_eval = StillsJudgeDetailEvaluator()
    from strands_evals.types.evaluation import EvaluationData

    for case in cases:
        result = qa_stills_judge_task(case)
        envelope = result["output"]
        assert envelope.get("tool") == "qa_stills_judge"
        expected = (case.metadata or {}).get("expected_verdict")
        assert envelope.get("verdict") == expected, (
            f"{case.name}: expected {expected}, got "
            f"{envelope.get('verdict')!r} ({envelope})"
        )

        eval_case = EvaluationData(
            input=case.input,
            expected_output=case.expected_output,
            actual_output=envelope,
            metadata=case.metadata,
        )
        for ev in (verdict_eval, detail_eval):
            outputs = ev.evaluate(eval_case)
            assert outputs and all(o.test_pass for o in outputs), (
                f"{case.name} failed {type(ev).__name__}: {[o.reason for o in outputs]}"
            )


@_REQUIRES_FFMPEG
def test_stills_judge_catches_frozen_grey_clip() -> None:
    """A solid-grey clip must score below the floor and verdict=fail."""
    cases = {c.name: c for c in qa_stills_judge_cases()}
    case = cases["frozen_grey_fails"]
    result = qa_stills_judge_task(case)
    envelope = result["output"]
    assert envelope["verdict"] == "fail"
    assert envelope["mean_pixel_delta"] < envelope["min_mean_pixel_delta"], envelope


@_REQUIRES_FFMPEG
def test_stills_judge_passes_real_motion_clip() -> None:
    """A mandelbrot-zoom clip must score above the floor and verdict=pass."""
    cases = {c.name: c for c in qa_stills_judge_cases()}
    case = cases["mandelbrot_motion_passes"]
    result = qa_stills_judge_task(case)
    envelope = result["output"]
    assert envelope["verdict"] == "pass", envelope
    assert envelope["mean_pixel_delta"] >= envelope["min_mean_pixel_delta"], envelope
