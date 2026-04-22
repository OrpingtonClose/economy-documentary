"""Content-analyst experiment factory.

Assembles the :class:`Experiment` that the strands-evals runner
consumes for ``docs/strands-migration/components/06-content-analyst.md``.
Five cases covering a vanilla five-scene movie, a data-heavy scene, a
short ten-second scene, a multi-voice scene, and a failure case where
the whisperx alignment is missing. Four-evaluator stack mirroring the
THRESHOLDS table.

The ``task`` callable supplied to :meth:`Experiment.run_evaluations`
is provided by whoever drives the run (CI, shadow runner, notebook)
so this module stays free of LLM calls and can be assembled
deterministically inside pytest.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.evaluators.faithfulness_evaluator import FaithfulnessEvaluator
from strands_evals.evaluators.output_evaluator import OutputEvaluator
from strands_evals.evaluators.trajectory_evaluator import TrajectoryEvaluator
from strands_evals.experiment import Experiment

from contracts import VISUAL_DIRECTION_CONTRACT
from strands_agents.evals.evaluators import ContractComplianceEvaluator


#: Minimum score per evaluator — mirrors ``eval-framework/THRESHOLDS.md``.
#: ``True`` in the second tuple position indicates a hard gate.
CONTENT_ANALYST_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "ContractComplianceEvaluator": (1.0, True),
    "TrajectoryEvaluator": (0.7, True),
    "OutputEvaluator": (0.75, False),
    "FaithfulnessEvaluator": (0.8, False),
}


#: Rubric handed to :class:`OutputEvaluator`. Scores the structural
#: integrity of the emitted ``content_analysis`` dict.
CONTENT_ANALYSIS_RUBRIC = (
    "The content_analysis must contain one entry per input scene under "
    "per_scene, and every entry must list at least one phrase. Every "
    "phrase must carry a stable phrase_id, a phrase_type drawn from "
    "{concept, entity, process, transition, data}, a narrative_weight "
    "drawn from {hook, build, payoff, connective}, a visual_intent "
    "sentence describing what a shot covering the phrase should depict "
    "(not a camera direction), a word_span into the scene's voices "
    "text, and a time_span that lies within the scene's whisperx "
    "segment bounds. The first scene's phrases must include at least "
    "one with narrative_weight=hook, and the last scene's phrases "
    "must include at least one with narrative_weight=payoff."
)


#: Rubric for :class:`TrajectoryEvaluator`. The content analyst must
#: call extract_phrases at least once per scene, call validate_phrases
#: at least once, and terminate with persist_content_analysis.
CONTENT_ANALYST_TRAJECTORY_RUBRIC = (
    "The content analyst must call extract_phrases at least once per "
    "scene, call validate_phrases at least once on the accumulated "
    "content_analysis before persistence, and end the trajectory with "
    "exactly one call to persist_content_analysis. No other tools may "
    "be invoked. When validate_phrases reports issues the agent may "
    "call extract_phrases again for the affected scenes before "
    "re-validating and persisting."
)


#: Tool descriptions shown to the judge LLM.
CONTENT_ANALYST_TRAJECTORY_DESCRIPTION = {
    "extract_phrases": (
        "Segment one scene's narration into phrases with phrase_type, "
        "narrative_weight, visual_intent, word_span, and time_span."
    ),
    "validate_phrases": (
        "Structural check over the accumulated content_analysis; "
        "returns issues the agent must address."
    ),
    "persist_content_analysis": (
        "Commit the final content_analysis structure onto agent state."
    ),
}


def _scene(
    scene_num: int,
    *,
    voices: list[tuple[str, str]],
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "scene_num": scene_num,
        "id": scene_num,
        "title": title or f"Scene {scene_num}",
        "voices": [
            {"voice_id": voice_id, "text": text}
            for voice_id, text in voices
        ],
    }


def _segment(
    scene_num: int,
    *,
    start: float,
    end: float,
) -> dict[str, Any]:
    return {
        "scene_num": scene_num,
        "start": start,
        "end": end,
        "text": f"scene-{scene_num} alignment",
    }


def _case_input(
    scenes: list[dict[str, Any]],
    whisperx_alignment: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "scenes": scenes,
        "whisperx_alignment": whisperx_alignment,
    }


def _five_scene_movie() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scenes = [
        _scene(
            1,
            voices=[
                (
                    "V1",
                    "Inflation is the rise in average prices across an economy.",
                )
            ],
        ),
        _scene(
            2,
            voices=[
                (
                    "V1",
                    "Central banks watch CPI and PPI readings to decide on rates.",
                )
            ],
        ),
        _scene(
            3,
            voices=[
                (
                    "V1",
                    "When rates rise, borrowing costs climb and demand cools.",
                )
            ],
        ),
        _scene(
            4,
            voices=[
                (
                    "V1",
                    "The same mechanism works in reverse when rates fall.",
                )
            ],
        ),
        _scene(
            5,
            voices=[
                (
                    "V1",
                    "So inflation, rates, and growth move together in a cycle.",
                )
            ],
        ),
    ]
    alignment = [
        _segment(1, start=0.0, end=6.5),
        _segment(2, start=6.5, end=13.0),
        _segment(3, start=13.0, end=20.0),
        _segment(4, start=20.0, end=26.0),
        _segment(5, start=26.0, end=34.0),
    ]
    return scenes, alignment


def content_analyst_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the five canonical content-analyst test cases.

    Each case's ``metadata`` records
    ``{"expected_scene_count", "min_phrases_per_scene"}`` so downstream
    evaluators (and shadow runners) can cross-check the produced
    ``content_analysis`` shape without re-reading the input scenes.
    """
    standard_scenes, standard_alignment = _five_scene_movie()
    data_heavy_scenes = [standard_scenes[0], standard_scenes[1], standard_scenes[4]]
    data_heavy_alignment = [
        standard_alignment[0],
        standard_alignment[1],
        standard_alignment[4],
    ]
    data_heavy_scenes[1] = _scene(
        2,
        voices=[
            (
                "V1",
                "Headline CPI rose 3.4 percent year-over-year while core CPI held "
                "at 3.6 percent, and PPI lagged at 2.1 percent — a mix that often "
                "presages a slowing pass-through cycle.",
            )
        ],
    )

    short_scene_scenes = [standard_scenes[0]]
    short_scene_alignment = [_segment(1, start=0.0, end=10.0)]

    multi_voice_scenes = [
        _scene(
            1,
            voices=[
                (
                    "V1",
                    "Inflation is measured across a basket of goods and services.",
                ),
                (
                    "V2",
                    "That basket is reweighted yearly to track household spending.",
                ),
            ],
        ),
        _scene(
            2,
            voices=[
                (
                    "V1",
                    "The result is a single index that policymakers target.",
                )
            ],
        ),
    ]
    multi_voice_alignment = [
        _segment(1, start=0.0, end=12.0),
        _segment(2, start=12.0, end=18.0),
    ]

    return [
        Case[dict[str, Any], dict[str, Any]](
            name="standard_5_scenes",
            session_id="content-analyst-case-001",
            input=_case_input(standard_scenes, standard_alignment),
            expected_trajectory=[
                "extract_phrases",
                "extract_phrases",
                "extract_phrases",
                "extract_phrases",
                "extract_phrases",
                "validate_phrases",
                "persist_content_analysis",
            ],
            metadata={
                "expected_scene_count": 5,
                "min_phrases_per_scene": 1,
            },
        ),
        Case[dict[str, Any], dict[str, Any]](
            name="data_heavy_scene",
            session_id="content-analyst-case-002",
            input=_case_input(data_heavy_scenes, data_heavy_alignment),
            expected_trajectory=[
                "extract_phrases",
                "extract_phrases",
                "extract_phrases",
                "validate_phrases",
                "persist_content_analysis",
            ],
            metadata={
                "expected_scene_count": 3,
                "min_phrases_per_scene": 1,
                "expected_phrase_types_at_least_one": ["data"],
            },
        ),
        Case[dict[str, Any], dict[str, Any]](
            name="short_scene_10s",
            session_id="content-analyst-case-003",
            input=_case_input(short_scene_scenes, short_scene_alignment),
            expected_trajectory=[
                "extract_phrases",
                "validate_phrases",
                "persist_content_analysis",
            ],
            metadata={
                "expected_scene_count": 1,
                "min_phrases_per_scene": 1,
                "max_phrase_duration_sec": 10.0,
            },
        ),
        Case[dict[str, Any], dict[str, Any]](
            name="multi_voice_scene",
            session_id="content-analyst-case-004",
            input=_case_input(multi_voice_scenes, multi_voice_alignment),
            expected_trajectory=[
                "extract_phrases",
                "extract_phrases",
                "validate_phrases",
                "persist_content_analysis",
            ],
            metadata={
                "expected_scene_count": 2,
                "min_phrases_per_scene": 1,
            },
        ),
        Case[dict[str, Any], dict[str, Any]](
            name="missing_alignment",
            session_id="content-analyst-case-005",
            input={
                "scenes": standard_scenes,
                "whisperx_alignment": [],
            },
            expected_trajectory=[],
            metadata={
                "expect_contract_violation": True,
            },
        ),
    ]


def content_analyst_evaluators() -> list[Evaluator[dict[str, Any], dict[str, Any]]]:
    """Return the evaluator stack applied to every content-analyst case.

    Order matters only for readability; the hard gates (contract,
    trajectory) appear first. The soft gates (output rubric,
    faithfulness) come after.
    """
    return [
        ContractComplianceEvaluator(VISUAL_DIRECTION_CONTRACT),
        TrajectoryEvaluator(
            rubric=CONTENT_ANALYST_TRAJECTORY_RUBRIC,
            trajectory_description=CONTENT_ANALYST_TRAJECTORY_DESCRIPTION,
        ),
        OutputEvaluator(rubric=CONTENT_ANALYSIS_RUBRIC),
        FaithfulnessEvaluator(),
    ]


def build_content_analyst_experiment() -> Experiment[
    dict[str, Any], dict[str, Any]
]:
    """Construct the :class:`Experiment` for Component 06."""
    return Experiment(
        cases=content_analyst_cases(),
        evaluators=content_analyst_evaluators(),
    )


def content_analyst_task(case: Case) -> dict[str, Any]:
    """Replay task adapter for the component-playground surface.

    Returns the case's canonical envelope so the evaluate endpoint can
    score a known-good payload against this component's evaluator
    stack without a live agent run. A live runner can replace this
    once provider plumbing lands in the playground.
    """
    metadata = case.metadata or {}
    return {
        "output": case.expected_output or {},
        "trajectory": list(
            case.expected_trajectory
            or metadata.get("canonical_trajectory")
            or []
        ),
        "metadata": {"mode": "replay", "case": case.name},
    }
