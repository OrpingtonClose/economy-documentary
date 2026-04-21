"""Scenario-refiner experiment factory.

Assembles the :class:`Experiment` that the strands-evals runner consumes
for ``docs/strands-migration/components/03-scenario-refiner.md``. Five
cases covering the no-op path (``timing_passed=True``), per-scene
shortening, per-scene lengthening, whole-movie target adjustment, and
pronunciation-hint preservation. Four-evaluator stack mirroring the
spec's THRESHOLDS table.

The ``task`` callable supplied to :meth:`Experiment.run_evaluations` is
provided by whoever drives the run (CI, shadow runner, notebook) so
this module stays free of LLM calls and can be built deterministically
inside pytest.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.deterministic.output import Contains
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.evaluators.output_evaluator import OutputEvaluator
from strands_evals.evaluators.trajectory_evaluator import TrajectoryEvaluator
from strands_evals.experiment import Experiment

from contracts import SCENARIO_CONTRACT
from strands_agents.evals.evaluators import ContractComplianceEvaluator


#: Minimum score per evaluator — mirrors ``eval-framework/THRESHOLDS.md``.
#: ``True`` in the second tuple position indicates a hard gate.
SCENARIO_REFINER_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "ContractComplianceEvaluator": (1.0, True),
    "Contains": (1.0, True),
    "OutputEvaluator": (0.75, False),
    "TrajectoryEvaluator": (0.7, False),
}


#: Rubric the :class:`OutputEvaluator` uses when scoring the refined
#: scenes JSON. Deliberately short; the evaluator judges whole-scene
#: integrity, not prose quality.
REFINER_OUTPUT_RUBRIC = (
    "The scenes JSON must contain the same set of scene IDs as the input, "
    "keep hook_spec on scene 1 and outro_spec on the final scene, preserve "
    "voices[].voice_id on every voice block, and retain pronunciation_hints "
    "on every scene. The narrative intent of each scene must remain "
    "recognizable even when narration is shortened or lengthened."
)


#: Rubric for :class:`TrajectoryEvaluator`. Purposely allows flexibility
#: in tool order because the LLM may choose to adjust durations,
#: rewrite text, or both, in any sequence before persisting.
REFINER_TRAJECTORY_RUBRIC = (
    "The refiner must always end its trajectory with persist_refined_scenes "
    "and should call validate_pronunciation_hints at least once before "
    "persisting when any narration was rewritten. Calls to adjust_scene_"
    "durations and tweak_voice_text may appear in any order. No other tools "
    "should be invoked. When timing already passed, the trajectory should "
    "contain zero successful tool calls."
)


#: Tool descriptions shown to the judge LLM.
REFINER_TRAJECTORY_DESCRIPTION = {
    "adjust_scene_durations": "Replace target_duration_sec per scene.",
    "tweak_voice_text": "Shorten or lengthen narration in a specific scene.",
    "validate_pronunciation_hints": "Verify pronunciation_hints are preserved.",
    "persist_refined_scenes": "Commit the refined scenes list to agent state.",
}


def _scene(
    scene_id: int,
    *,
    target: float,
    voices: list[tuple[str, str]],
    hints: dict[str, str] | None = None,
    hook_spec: dict[str, Any] | None = None,
    outro_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal scene dict for eval cases."""
    scene: dict[str, Any] = {
        "id": scene_id,
        "scene_num": scene_id,
        "title": f"Scene {scene_id}",
        "target_duration_sec": target,
        "voices": [{"voice_id": voice_id, "text": text} for voice_id, text in voices],
        "pronunciation_hints": dict(hints or {}),
    }
    if hook_spec is not None:
        scene["hook_spec"] = hook_spec
    if outro_spec is not None:
        scene["outro_spec"] = outro_spec
    return scene


_HINTS = {"GDP": "G. D. P.", "CPI": "C. P. I."}
_HOOK_SPEC = {
    "topic_specific_motif": "market ticker",
    "motion_description": "zoom across headline chyron",
    "narrative_pull": "show why prices matter",
}
_OUTRO_SPEC = {
    "closing_shot": "city skyline at dusk",
    "recap_sentence": "Inflation remains the central puzzle.",
    "cta": "Subscribe for the next episode.",
    "brand_card": "Economy Documentary",
}


def _five_scene_input() -> list[dict[str, Any]]:
    return [
        _scene(
            1,
            target=60.0,
            voices=[("V1", "Inflation means prices trend upward across an economy.")],
            hints=_HINTS,
            hook_spec=_HOOK_SPEC,
        ),
        _scene(
            2,
            target=60.0,
            voices=[("V1", "GDP growth and CPI readings move in tandem here.")],
            hints=_HINTS,
        ),
        _scene(
            3,
            target=60.0,
            voices=[("V1", "Central banks respond by changing short-term rates.")],
            hints=_HINTS,
        ),
        _scene(
            4,
            target=60.0,
            voices=[
                ("V1", "Households and firms adjust their expectations accordingly.")
            ],
            hints=_HINTS,
        ),
        _scene(
            5,
            target=60.0,
            voices=[("V1", "Stable prices let markets coordinate over time.")],
            hints=_HINTS,
            outro_spec=_OUTRO_SPEC,
        ),
    ]


def refiner_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the five canonical refiner test cases."""
    return [
        Case[dict[str, Any], dict[str, Any]](
            name="timing_passed_noop",
            session_id="refiner-case-001",
            input={
                "scenes": _five_scene_input(),
                "timing_passed": True,
                "timing_report": {},
                "target_duration_sec": 300.0,
            },
            expected_trajectory=[],
            metadata={
                "expect_noop": True,
                "expect_scene_count": 5,
            },
        ),
        Case[dict[str, Any], dict[str, Any]](
            name="shorten_single_scene",
            session_id="refiner-case-002",
            input={
                "scenes": _five_scene_input(),
                "timing_passed": False,
                "timing_report": {
                    "violations": [
                        {"scene_id": 3, "deviation_sec": 12.0, "reason": "scene over"},
                    ],
                    "per_scene": {"3": {"actual": 72.0, "target": 60.0}},
                },
                "target_duration_sec": 300.0,
            },
            expected_trajectory=[
                "tweak_voice_text",
                "validate_pronunciation_hints",
                "persist_refined_scenes",
            ],
            metadata={
                "expect_scene_count": 5,
                "expect_shorter_scene_id": 3,
            },
        ),
        Case[dict[str, Any], dict[str, Any]](
            name="lengthen_single_scene",
            session_id="refiner-case-003",
            input={
                "scenes": _five_scene_input(),
                "timing_passed": False,
                "timing_report": {
                    "violations": [
                        {
                            "scene_id": 2,
                            "deviation_sec": -11.0,
                            "reason": "scene under",
                        },
                    ],
                    "per_scene": {"2": {"actual": 49.0, "target": 60.0}},
                },
                "target_duration_sec": 300.0,
            },
            expected_trajectory=[
                "tweak_voice_text",
                "validate_pronunciation_hints",
                "persist_refined_scenes",
            ],
            metadata={
                "expect_scene_count": 5,
                "expect_longer_scene_id": 2,
            },
        ),
        Case[dict[str, Any], dict[str, Any]](
            name="total_off_per_scene_ok",
            session_id="refiner-case-004",
            input={
                "scenes": _five_scene_input(),
                "timing_passed": False,
                "timing_report": {
                    "violations": [
                        {
                            "scene_id": None,
                            "deviation_sec": 36.0,
                            "reason": "movie over",
                        },
                    ],
                    "per_scene": {
                        str(i): {"actual": 67.2, "target": 60.0} for i in range(1, 6)
                    },
                },
                "target_duration_sec": 300.0,
            },
            expected_trajectory=[
                "adjust_scene_durations",
                "validate_pronunciation_hints",
                "persist_refined_scenes",
            ],
            metadata={
                "expect_scene_count": 5,
                "expect_all_scenes_updated": True,
            },
        ),
        Case[dict[str, Any], dict[str, Any]](
            name="preserve_pronunciation_hints",
            session_id="refiner-case-005",
            input={
                "scenes": _five_scene_input(),
                "timing_passed": False,
                "timing_report": {
                    "violations": [
                        {"scene_id": 1, "deviation_sec": -8.0, "reason": "scene under"},
                    ],
                    "per_scene": {"1": {"actual": 52.0, "target": 60.0}},
                },
                "target_duration_sec": 300.0,
            },
            expected_trajectory=[
                "tweak_voice_text",
                "validate_pronunciation_hints",
                "persist_refined_scenes",
            ],
            metadata={
                "expect_scene_count": 5,
                "expect_hints_preserved": True,
            },
        ),
    ]


def refiner_evaluators() -> list[Evaluator[dict[str, Any], dict[str, Any]]]:
    """Return the evaluator stack applied to every refiner case."""
    return [
        ContractComplianceEvaluator(SCENARIO_CONTRACT),
        Contains("pronunciation_hints"),
        OutputEvaluator(rubric=REFINER_OUTPUT_RUBRIC),
        TrajectoryEvaluator(
            rubric=REFINER_TRAJECTORY_RUBRIC,
            trajectory_description=REFINER_TRAJECTORY_DESCRIPTION,
        ),
    ]


def build_refiner_experiment() -> Experiment[dict[str, Any], dict[str, Any]]:
    """Construct the :class:`Experiment` for Component 03."""
    return Experiment(cases=refiner_cases(), evaluators=refiner_evaluators())
