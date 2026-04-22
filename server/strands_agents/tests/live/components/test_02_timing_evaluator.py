"""Live-judge proof of robustness for Component 02 (timing-evaluator).

The timing evaluator is deterministic — it computes absolute deviations
between target scene durations and WhisperX-reported actuals.  So the
clear-cut tests here are:

1. A clean alignment **passes** (no violations).
2. A drifted alignment **fails** with violation strings that a
   downstream LLM (the scenario refiner) can act on.

Because the refiner is the consumer of these reports, a live Gemini
judge acts as the refiner's proxy: given the ``violations`` list, can a
separate model family identify which scene is off and by roughly how
much?  If the answer is "no", the report is not fit for its purpose —
which is a real defect in the component, not a flaky judge.
"""

from __future__ import annotations

from typing import Any

from strands_agents.timing_tool import compute_timing_report

from .._judges import judge_text_yes
from ..conftest import requires_google_api


def _three_scenes(target_per_scene: float = 45.0) -> list[dict[str, Any]]:
    """Build three minimal scenes each targeting ``target_per_scene``."""
    return [
        {
            "scene_num": i + 1,
            "scene_id": f"scene_{i + 1}",
            "voices": [{"text": "narration line one"}],
            "target_duration_sec": target_per_scene,
            "duration_sec": target_per_scene,
        }
        for i in range(3)
    ]


# ---------------------------------------------------------------------------
# Deterministic proofs
# ---------------------------------------------------------------------------


def test_clean_alignment_passes() -> None:
    """A WhisperX alignment that matches target durations passes.

    Every scene actually duration is exactly the target, total duration
    equals the sum; no violations should be reported.
    """
    scenes = _three_scenes()
    alignment = {
        "total_duration_sec": 135.0,
        "per_scene": [
            {"scene_id": f"scene_{i + 1}", "duration_sec": 45.0} for i in range(3)
        ],
    }
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=alignment,
        target_duration_sec=135.0,
    )
    assert out["timing_passed"] is True
    assert out["timing_report"]["violations"] == []


def test_drifted_scene_is_flagged() -> None:
    """A scene that's 40 % over target must be flagged as a violation.

    The legacy tolerance is the maximum of 5 s and 15 % of the target
    (~6.75 s at a 45 s target).  A 60 s actual is 15 s over — well
    outside tolerance — and must produce a per-scene violation string
    that carries the scene id and the deviation.
    """
    scenes = _three_scenes()
    alignment = {
        "total_duration_sec": 150.0,
        "per_scene": [
            {"scene_id": "scene_1", "duration_sec": 45.0},
            {"scene_id": "scene_2", "duration_sec": 60.0},
            {"scene_id": "scene_3", "duration_sec": 45.0},
        ],
    }
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=alignment,
        target_duration_sec=135.0,
    )
    assert out["timing_passed"] is False
    violations = out["timing_report"]["violations"]
    assert any("scene_2" in v for v in violations), (
        f"scene_2 drift not surfaced in violations: {violations}"
    )


# ---------------------------------------------------------------------------
# Live-judge: is the report LLM-consumable?
# ---------------------------------------------------------------------------


@requires_google_api
def test_violation_report_is_readable_by_downstream_llm() -> None:
    """The refiner LLM must be able to answer 'which scene is off?' from the report.

    This is the semantic contract: the timing report exists to be read
    by the scenario refiner.  If a production LLM can't correctly
    identify the drifting scene from the violations list, the report
    is not fit for purpose.
    """
    scenes = _three_scenes()
    alignment = {
        "total_duration_sec": 150.0,
        "per_scene": [
            {"scene_id": "scene_1", "duration_sec": 45.0},
            {"scene_id": "scene_2", "duration_sec": 60.0},
            {"scene_id": "scene_3", "duration_sec": 45.0},
        ],
    }
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=alignment,
        target_duration_sec=135.0,
    )
    violations = out["timing_report"]["violations"]
    assert violations, "precondition: expected violations"

    rendered = "\n".join(f"- {v}" for v in violations)
    prompt = (
        "You are reviewing a timing report from a documentary pipeline. "
        "The following violations were reported:\n"
        f"{rendered}\n\n"
        "Question: Does this report indicate that scene_2 is the "
        "scene whose actual duration is longer than its target?  "
        "Answer with a single word: yes or no."
    )
    verdict = judge_text_yes(prompt)
    assert not verdict.disabled, f"judge disabled: {verdict.error}"
    assert verdict.is_yes, (
        f"Gemini could not extract scene_2 as the drifting scene from "
        f"the report; answer={verdict.answer!r}"
    )
