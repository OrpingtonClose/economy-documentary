"""Visual-loop experiment factory for component 09.

The visual loop is **not** a runtime module — it is a trajectory the
``visual`` SubAgent follows using the Strands leaves from components 06
(content-analyst), 07 (visual-concepter) and 08 (coherence-evaluator).
This experiment pins the expected trajectory and scores the SubAgent
against five canonical cases.

Cases:

1. ``one_shot_good`` — 3 scenes, first scoring passes → 1 iteration,
   no revisions, verdict GOOD.
2. ``one_revise`` — 5 scenes, scene 3 flagged as off-style on iter 1,
   regenerated on iter 2 → 2 iterations, 1 revision, verdict GOOD.
3. ``persistent_fair`` — 5 scenes, revision never converges → 5
   iterations, delegates to escalation on cap.
4. ``analyst_fails`` — ``extract_phrases`` raises on scene 1 → 0
   iterations, SubAgent surfaces the error and delegates to
   escalation without persisting any content analysis or concepts.
5. ``style_lock_drift`` — 4 scenes, concepts 2 and 4 drift on iter 1,
   regenerated on iter 2 → 2 iterations, 2 revised scenes, verdict
   GOOD.

Evaluator stack mirrors ``eval-framework/THRESHOLDS.md``:

- :class:`VisualLoopTrajectoryEvaluator` (hard gate ≥0.80).
- :class:`ContractComplianceEvaluator` (hard gate 1.00) against
  :data:`VISUAL_DIRECTION_CONTRACT`.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment

from contracts import VISUAL_DIRECTION_CONTRACT
from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
    VisualLoopTrajectoryEvaluator,
)
from strands_agents.subagents.visual import VISUAL_LOOP_MAX_ITERATIONS

#: Minimum per-evaluator score / hard-gate flags. Mirrors the visual
#: stage thresholds in ``eval-framework/THRESHOLDS.md``.
VISUAL_LOOP_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "VisualLoopTrajectoryEvaluator": (0.80, True),
    "ContractComplianceEvaluator": (1.00, True),
}


# ---------------------------------------------------------------------------
# Trajectory helpers
# ---------------------------------------------------------------------------


def _extract_phrases_call(scene_num: int, turn: int) -> dict[str, Any]:
    return {
        "name": "extract_phrases",
        "at_turn": turn,
        "args": {"scene_num": scene_num},
    }


def _validate_phrases_call(turn: int) -> dict[str, Any]:
    return {"name": "validate_phrases", "at_turn": turn, "args": {}}


def _persist_content_analysis_call(turn: int) -> dict[str, Any]:
    return {"name": "persist_content_analysis", "at_turn": turn, "args": {}}


def _propose_concept_call(
    scene_num: int, phrase_idx: int, turn: int, *, revision: int = 1
) -> dict[str, Any]:
    return {
        "name": "propose_concept",
        "at_turn": turn,
        "args": {
            "scene_num": scene_num,
            "phrase_idx": phrase_idx,
            "revision": revision,
        },
    }


def _check_style_lock_call(turn: int) -> dict[str, Any]:
    return {"name": "check_style_lock", "at_turn": turn, "args": {}}


def _persist_visual_concepts_call(turn: int) -> dict[str, Any]:
    return {"name": "persist_visual_concepts", "at_turn": turn, "args": {}}


def _score_call(turn: int, *, rating: str) -> dict[str, Any]:
    return {
        "name": "score_visual_coherence",
        "at_turn": turn,
        "args": {"expected_rating": rating},
    }


def _persist_report_call(turn: int) -> dict[str, Any]:
    return {"name": "persist_coherence_report", "at_turn": turn, "args": {}}


def _delegate_escalation_call(turn: int, *, reason: str) -> dict[str, Any]:
    return {
        "name": "task",
        "at_turn": turn,
        "args": {
            "subagent_type": "escalation",
            "description": reason,
        },
    }


def _bootstrap_trajectory(
    scene_count: int, *, start_turn: int = 1
) -> tuple[list[dict[str, Any]], int]:
    """Build the iteration-1 bootstrap (extract → validate → persist)."""
    trajectory: list[dict[str, Any]] = [
        _extract_phrases_call(i + 1, turn=start_turn)
        for i in range(scene_count)
    ]
    trajectory.append(_validate_phrases_call(turn=start_turn + 1))
    trajectory.append(_persist_content_analysis_call(turn=start_turn + 2))
    return trajectory, start_turn + 3


def _concept_iteration_trajectory(
    *,
    scene_phrase_pairs: list[tuple[int, int]],
    start_turn: int,
    revision: int,
    rating: str,
) -> tuple[list[dict[str, Any]], int]:
    """Build one iteration: propose+ → check → persist → score → persist."""
    trajectory: list[dict[str, Any]] = [
        _propose_concept_call(
            scene_num, phrase_idx, turn=start_turn, revision=revision
        )
        for scene_num, phrase_idx in scene_phrase_pairs
    ]
    trajectory.append(_check_style_lock_call(turn=start_turn + 1))
    trajectory.append(_persist_visual_concepts_call(turn=start_turn + 2))
    trajectory.append(_score_call(turn=start_turn + 3, rating=rating))
    trajectory.append(_persist_report_call(turn=start_turn + 4))
    return trajectory, start_turn + 5


# ---------------------------------------------------------------------------
# Contract state helpers
# ---------------------------------------------------------------------------


def _scenes(n: int) -> list[dict[str, Any]]:
    return [{"id": i + 1, "target_duration_sec": 12.0} for i in range(n)]


def _whisperx_alignment(n: int) -> dict[str, Any]:
    return {
        "total_duration_sec": 12.0 * n,
        "per_clip": {
            str(i + 1): {"start": i * 12.0, "end": (i + 1) * 12.0}
            for i in range(n)
        },
    }


def _content_analysis(n: int) -> dict[str, Any]:
    return {
        "per_scene": [
            {
                "scene_num": i + 1,
                "phrases": [
                    {
                        "phrase_id": f"s{i + 1}_p0",
                        "text": "phrase",
                        "phrase_type": "concept",
                        "narrative_weight": "build",
                        "visual_intent": "static shot",
                        "word_span": [0, 5],
                        "time_span": [i * 12.0, (i + 1) * 12.0],
                    }
                ],
            }
            for i in range(n)
        ]
    }


def _visual_concepts(n: int) -> list[dict[str, Any]]:
    return [
        {
            "scene_num": i + 1,
            "phrase_id": f"s{i + 1}_p0",
            "concept": f"concept for scene {i + 1}",
            "shot_type": "wide",
            "camera_movement": "static",
        }
        for i in range(n)
    ]


def _visual_coherence_report(
    rating: str,
    *,
    passed: bool,
) -> dict[str, Any]:
    return {
        "rating": rating,
        "issues": [] if passed else ["style drift on scene"],
        "suggestions": [] if passed else ["retry with tighter style_lock"],
        "visual_coherence_passed": passed,
    }


def _state_after_visual(
    *,
    scenes: list[dict[str, Any]],
    rating: str,
    passed: bool,
    include_concepts: bool = True,
) -> dict[str, Any]:
    n = len(scenes)
    state: dict[str, Any] = {
        "scenes": scenes,
        "whisperx_alignment": _whisperx_alignment(n),
        "content_analysis": _content_analysis(n),
    }
    if include_concepts:
        state["visual_concepts"] = _visual_concepts(n)
        state["visual_coherence_report"] = _visual_coherence_report(
            rating, passed=passed
        )
    return state


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def _one_shot_good() -> Case:
    scenes = _scenes(3)
    bootstrap, turn = _bootstrap_trajectory(3)
    iter_1, turn = _concept_iteration_trajectory(
        scene_phrase_pairs=[(1, 0), (2, 0), (3, 0)],
        start_turn=turn,
        revision=1,
        rating="GOOD",
    )
    trajectory = bootstrap + iter_1
    return Case(
        name="one_shot_good",
        input={"scenes": scenes, "style_lock": {"dominant_style": "hand_drawn"}},
        expected_output={"coherence_verdict": "GOOD", "iterations": 1},
        expected_trajectory=trajectory,
        metadata={
            "case_name": "one_shot_good",
            "final_state": _state_after_visual(
                scenes=scenes, rating="GOOD", passed=True
            ),
            "expected_iterations": 1,
            "expected_scene_count": 3,
            "expected_revision_counts": [],
            "expects_pass": True,
            "expects_delegation": False,
            "contract_name": "visual_direction",
        },
    )


def _one_revise() -> Case:
    scenes = _scenes(5)
    bootstrap, turn = _bootstrap_trajectory(5)
    iter_1, turn = _concept_iteration_trajectory(
        scene_phrase_pairs=[(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)],
        start_turn=turn,
        revision=1,
        rating="FAIR",
    )
    # Iteration 2 targets only scene 3 — the weak scene flagged by the
    # iter-1 coherence report.
    iter_2, turn = _concept_iteration_trajectory(
        scene_phrase_pairs=[(3, 0)],
        start_turn=turn,
        revision=2,
        rating="GOOD",
    )
    trajectory = bootstrap + iter_1 + iter_2
    return Case(
        name="one_revise",
        input={"scenes": scenes, "style_lock": {"dominant_style": "cinematic"}},
        expected_output={"coherence_verdict": "GOOD", "iterations": 2},
        expected_trajectory=trajectory,
        metadata={
            "case_name": "one_revise",
            "final_state": _state_after_visual(
                scenes=scenes, rating="GOOD", passed=True
            ),
            "expected_iterations": 2,
            "expected_scene_count": 5,
            "expected_revision_counts": [1],
            "expects_pass": True,
            "expects_delegation": False,
            "contract_name": "visual_direction",
        },
    )


def _persistent_fair() -> Case:
    scenes = _scenes(5)
    bootstrap, turn = _bootstrap_trajectory(5)
    # Iteration 1 proposes for every scene; iterations 2..5 revise
    # scene 3 each time but coherence stays FAIR. Hitting the cap
    # forces the SubAgent to delegate to escalation.
    iter_1, turn = _concept_iteration_trajectory(
        scene_phrase_pairs=[(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)],
        start_turn=turn,
        revision=1,
        rating="FAIR",
    )
    trajectory = bootstrap + iter_1
    for iteration in range(2, VISUAL_LOOP_MAX_ITERATIONS + 1):
        iter_n, turn = _concept_iteration_trajectory(
            scene_phrase_pairs=[(3, 0)],
            start_turn=turn,
            revision=iteration,
            rating="FAIR",
        )
        trajectory += iter_n
    trajectory.append(
        _delegate_escalation_call(
            turn=turn,
            reason=(
                f"visual loop cap reached ({VISUAL_LOOP_MAX_ITERATIONS} "
                "iterations); coherence stuck at FAIR"
            ),
        )
    )
    return Case(
        name="persistent_fair",
        input={"scenes": scenes, "style_lock": {"dominant_style": "cinematic"}},
        expected_output={"coherence_verdict": "FAIR", "escalated": True},
        expected_trajectory=trajectory,
        metadata={
            "case_name": "persistent_fair",
            # Contract is still satisfied — we persist the best-scoring
            # concepts + a FAIR report before delegating.
            "final_state": _state_after_visual(
                scenes=scenes, rating="FAIR", passed=False
            ),
            "expected_iterations": VISUAL_LOOP_MAX_ITERATIONS,
            "expected_scene_count": 5,
            "expected_revision_counts": [1] * (VISUAL_LOOP_MAX_ITERATIONS - 1),
            "expects_pass": False,
            "expects_delegation": True,
            "contract_name": "visual_direction",
        },
    )


def _analyst_fails() -> Case:
    scenes = _scenes(3)
    # Only one extract_phrases — the first scene — before the SubAgent
    # surfaces the error. No validate_phrases, no persist, no concepts,
    # no scoring. Straight to delegation.
    trajectory: list[dict[str, Any]] = [
        _extract_phrases_call(1, turn=1),
        _delegate_escalation_call(
            turn=2,
            reason=(
                "extract_phrases raised on scene 1; cannot build content "
                "analysis, aborting visual loop"
            ),
        ),
    ]
    # Contract final state lacks content_analysis/visual_concepts — the
    # ContractComplianceEvaluator should flag the failure. We ship the
    # *expected* failure shape so the evaluator records the gap.
    final_state = {
        "scenes": scenes,
        "whisperx_alignment": _whisperx_alignment(3),
    }
    return Case(
        name="analyst_fails",
        input={"scenes": scenes, "style_lock": {"dominant_style": "cinematic"}},
        expected_output={"error": "extract_phrases_failed"},
        expected_trajectory=trajectory,
        metadata={
            "case_name": "analyst_fails",
            "final_state": final_state,
            "expected_iterations": 0,
            "expected_scene_count": 3,
            "expected_revision_counts": [],
            "expects_pass": False,
            "expects_delegation": True,
            # Analyst-failure case intentionally violates the contract;
            # the contract-compliance evaluator is suppressed for this
            # case by omitting the ``contract_name`` hint.
        },
    )


def _style_lock_drift() -> Case:
    scenes = _scenes(4)
    bootstrap, turn = _bootstrap_trajectory(4)
    iter_1, turn = _concept_iteration_trajectory(
        scene_phrase_pairs=[(1, 0), (2, 0), (3, 0), (4, 0)],
        start_turn=turn,
        revision=1,
        rating="FAIR",
    )
    iter_2, turn = _concept_iteration_trajectory(
        scene_phrase_pairs=[(2, 0), (4, 0)],
        start_turn=turn,
        revision=2,
        rating="GOOD",
    )
    trajectory = bootstrap + iter_1 + iter_2
    return Case(
        name="style_lock_drift",
        input={"scenes": scenes, "style_lock": {"dominant_style": "hand_drawn"}},
        expected_output={"coherence_verdict": "GOOD", "iterations": 2},
        expected_trajectory=trajectory,
        metadata={
            "case_name": "style_lock_drift",
            "final_state": _state_after_visual(
                scenes=scenes, rating="GOOD", passed=True
            ),
            "expected_iterations": 2,
            "expected_scene_count": 4,
            "expected_revision_counts": [2],
            "expects_pass": True,
            "expects_delegation": False,
            "contract_name": "visual_direction",
        },
    )


# ---------------------------------------------------------------------------
# Public factories
# ---------------------------------------------------------------------------


def visual_loop_cases() -> list[Case]:
    """Return the five canonical visual-loop trajectory cases."""
    return [
        _one_shot_good(),
        _one_revise(),
        _persistent_fair(),
        _analyst_fails(),
        _style_lock_drift(),
    ]


def visual_loop_evaluators() -> list[Evaluator]:
    """Return the visual-loop evaluator stack in spec order."""
    return [
        VisualLoopTrajectoryEvaluator(),
        ContractComplianceEvaluator(VISUAL_DIRECTION_CONTRACT),
    ]


def build_visual_loop_experiment() -> Experiment:
    """Build the visual-loop experiment for the strands-evals runner."""
    return Experiment(
        cases=visual_loop_cases(),
        evaluators=visual_loop_evaluators(),
    )


__all__ = [
    "VISUAL_LOOP_EVALUATOR_THRESHOLDS",
    "build_visual_loop_experiment",
    "visual_loop_cases",
    "visual_loop_evaluators",
]
