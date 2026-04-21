"""Coherence-evaluator experiment factory.

Assembles the :class:`Experiment` that the strands-evals runner
consumes for ``docs/strands-migration/components/08-coherence-evaluator.md``.
Five cases covering clean concepts, a style_lock violation, repetitive
shot runs, a phrase missing its visual concept, and a minor palette
drift. Three-evaluator stack mirroring the THRESHOLDS table.

The ``task`` callable supplied to :meth:`Experiment.run_evaluations`
is provided by whoever drives the run (CI, shadow runner, notebook)
so this module stays free of LLM calls and can be assembled
deterministically inside pytest.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.evaluators.output_evaluator import OutputEvaluator
from strands_evals.experiment import Experiment

from contracts import VISUAL_DIRECTION_CONTRACT
from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
    VisualCoherenceEvaluator,
)


#: Minimum score per evaluator — mirrors ``eval-framework/THRESHOLDS.md``.
#: ``True`` in the second tuple position indicates a hard gate.
COHERENCE_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "ContractComplianceEvaluator": (1.0, True),
    "OutputEvaluator": (0.75, False),
    "VisualCoherenceEvaluator": (0.75, False),
}


#: Rubric handed to :class:`OutputEvaluator`. Scores the structural
#: integrity of the emitted ``visual_coherence_report`` dict.
COHERENCE_REPORT_RUBRIC = (
    "The visual_coherence_report must contain a rating drawn from "
    "{EXCELLENT, GOOD, FAIR, POOR, UNKNOWN}, an issues list (possibly "
    "empty), a suggestions list (possibly empty), and a "
    "visual_coherence_passed bool that equals True iff rating is "
    "EXCELLENT or GOOD. If the rating is POOR the issues list must be "
    "non-empty and explain at least one concrete invariant break "
    "(style_lock violation, missing visual for a phrase, >3 consecutive "
    "identical shots). If the rating is FAIR the issues list must name "
    "the specific drift (palette shift, repetitive shot, narrative "
    "mismatch) rather than a generic 'could be better' sentence."
)


#: Rubric for ``TrajectoryEvaluator`` consumers that want a trajectory
#: rubric (the experiment itself does not include TrajectoryEvaluator
#: by default — evaluators are picked per the THRESHOLDS table).
COHERENCE_TRAJECTORY_RUBRIC = (
    "The coherence evaluator must call score_visual_coherence exactly "
    "once, then call persist_coherence_report exactly once. No other "
    "tools may be invoked. The agent must not retry scoring; a single "
    "pass is sufficient because the score merges deterministic "
    "structural checks with the LLM soft judgement."
)


#: Tool descriptions shown to tool-selection judges if the CI runner
#: layers one on top of the default evaluator stack.
COHERENCE_TOOL_DESCRIPTIONS = {
    "score_visual_coherence": (
        "Rate visual_concepts for coherence against style_lock and "
        "content_analysis. Layers deterministic structural checks over "
        "an LLM soft judgement; any hard invariant forces POOR."
    ),
    "persist_coherence_report": (
        "Commit the final visual_coherence_report onto agent.state. "
        "Terminal tool — stop after this call returns successfully."
    ),
}


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _phrase(
    scene_id: int,
    phrase_idx: int,
    *,
    text: str,
    phrase_type: str = "concept",
    narrative_weight: str = "build",
    time_span: tuple[float, float] = (0.0, 3.0),
) -> dict[str, Any]:
    pid = f"ph-{scene_id:02d}-{phrase_idx:02d}-{abs(hash(text)) % (10**10):010d}"
    return {
        "phrase_id": pid,
        "scene_id": scene_id,
        "scene_num": scene_id,
        "phrase_type": phrase_type,
        "narrative_weight": narrative_weight,
        "visual_intent": text,
        "text": text,
        "word_span": [0, len(text.split())],
        "time_span": list(time_span),
    }


def _scene_entry(
    scene_id: int, phrases: list[dict[str, Any]]
) -> dict[str, Any]:
    return {"scene_num": scene_id, "phrases": phrases}


def _content_analysis(
    scene_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {"per_scene": scene_entries}


def _style_lock() -> dict[str, Any]:
    return {
        "dominant_style": "cinematic_documentary",
        "positive_fragment": "shot on 35mm film with shallow depth of field",
        "negative_fragment": "cartoon, anime, illustration",
        "forbidden_styles": ["anime", "cartoon", "cyberpunk", "vaporwave"],
        "palette": ["warm tungsten", "soft daylight"],
        "realism_anchors": ["4K", "no CGI"],
    }


def _concept(
    phrase: dict[str, Any],
    *,
    shot_type: str = "medium",
    camera_movement: str = "dolly_in",
    prompt_extra: str = "",
    style_lock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lock = style_lock or _style_lock()
    positive = lock["positive_fragment"]
    prompt = (
        f"{positive}. {phrase['visual_intent']} {prompt_extra}".strip()
    )
    duration = float(phrase["time_span"][1] - phrase["time_span"][0])
    return {
        "phrase_id": phrase["phrase_id"],
        "scene_id": phrase["scene_id"],
        "shot_type": shot_type,
        "camera_movement": camera_movement,
        "prompt": prompt,
        "negative_prompt": (
            "cartoon, anime, illustration, text, watermark, "
            "logo, low resolution, artifacts"
        ),
        "duration_sec": max(1.0, min(10.0, duration)),
        "ltx_params": {
            "resolution": [1280, 720],
            "seed": None,
            "steps": 30,
        },
        "style_lock_applied": True,
    }


def _case_input(
    visual_concepts: list[dict[str, Any]],
    style_lock: dict[str, Any],
    content_analysis: dict[str, Any],
    *,
    scenes: list[dict[str, Any]] | None = None,
    whisperx_alignment: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "visual_concepts": visual_concepts,
        "style_lock": style_lock,
        "content_analysis": content_analysis,
    }
    if scenes is not None:
        payload["scenes"] = scenes
    if whisperx_alignment is not None:
        payload["whisperx_alignment"] = whisperx_alignment
    return payload


def _scenes_for(content_analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive the minimal scenes list the VISUAL_DIRECTION contract wants."""
    scenes: list[dict[str, Any]] = []
    for entry in content_analysis.get("per_scene") or []:
        scene_num = entry.get("scene_num")
        scenes.append(
            {
                "scene_num": scene_num,
                "id": scene_num,
                "title": f"Scene {scene_num}",
                "voices": [
                    {
                        "voice_id": "V1",
                        "text": " ".join(
                            p["text"] for p in entry.get("phrases", [])
                        ),
                    }
                ],
            }
        )
    return scenes


def _alignment_for(
    content_analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    """Derive a minimal whisperx alignment covering the content analysis."""
    segments: list[dict[str, Any]] = []
    for entry in content_analysis.get("per_scene") or []:
        scene_num = entry.get("scene_num")
        phrases = entry.get("phrases") or []
        if not phrases:
            continue
        start = float(phrases[0]["time_span"][0])
        end = float(phrases[-1]["time_span"][1])
        segments.append(
            {
                "scene_num": scene_num,
                "start": start,
                "end": end,
                "text": f"scene-{scene_num} alignment",
            }
        )
    return segments


# ---------------------------------------------------------------------------
# Case builders
# ---------------------------------------------------------------------------


def _five_scene_analysis() -> dict[str, Any]:
    return _content_analysis(
        [
            _scene_entry(
                1,
                [
                    _phrase(
                        1,
                        0,
                        text="An empty street at dawn, lanterns still on.",
                        phrase_type="concept",
                        narrative_weight="hook",
                        time_span=(0.0, 3.0),
                    )
                ],
            ),
            _scene_entry(
                2,
                [
                    _phrase(
                        2,
                        0,
                        text="Traders arrive as the market opens.",
                        phrase_type="process",
                        narrative_weight="build",
                        time_span=(3.0, 6.0),
                    )
                ],
            ),
            _scene_entry(
                3,
                [
                    _phrase(
                        3,
                        0,
                        text="Price tags flip and settle.",
                        phrase_type="data",
                        narrative_weight="build",
                        time_span=(6.0, 9.0),
                    )
                ],
            ),
            _scene_entry(
                4,
                [
                    _phrase(
                        4,
                        0,
                        text="A family compares bread and milk.",
                        phrase_type="concept",
                        narrative_weight="connective",
                        time_span=(9.0, 12.0),
                    )
                ],
            ),
            _scene_entry(
                5,
                [
                    _phrase(
                        5,
                        0,
                        text="The same street at dusk, receipts in hand.",
                        phrase_type="concept",
                        narrative_weight="payoff",
                        time_span=(12.0, 15.0),
                    )
                ],
            ),
        ]
    )


def _clean_concepts_case() -> Case[dict[str, Any], dict[str, Any]]:
    analysis = _five_scene_analysis()
    phrases = [
        entry["phrases"][0] for entry in analysis["per_scene"]
    ]
    shots = ["wide", "medium", "insert", "medium_close_up", "wide"]
    moves = ["locked", "dolly_in", "graphic_overlay", "handheld", "orbit"]
    concepts = [
        _concept(p, shot_type=s, camera_movement=m)
        for p, s, m in zip(phrases, shots, moves, strict=True)
    ]
    return Case[dict[str, Any], dict[str, Any]](
        name="clean_concepts",
        session_id="coherence-evaluator-case-001",
        input=_case_input(
            concepts,
            _style_lock(),
            analysis,
            scenes=_scenes_for(analysis),
            whisperx_alignment=_alignment_for(analysis),
        ),
        expected_trajectory=[
            "score_visual_coherence",
            "persist_coherence_report",
        ],
        metadata={
            "expected_rating_in": ["EXCELLENT", "GOOD"],
            "expected_passed": True,
            "expected_hard_violations": 0,
        },
    )


def _style_lock_violation_case() -> Case[dict[str, Any], dict[str, Any]]:
    analysis = _five_scene_analysis()
    phrases = [
        entry["phrases"][0] for entry in analysis["per_scene"]
    ]
    style_lock = _style_lock()
    shots = ["wide", "medium", "insert", "medium_close_up", "wide"]
    moves = ["locked", "dolly_in", "graphic_overlay", "handheld", "orbit"]
    concepts = [
        _concept(p, shot_type=s, camera_movement=m)
        for p, s, m in zip(phrases, shots, moves, strict=True)
    ]
    # Inject a forbidden style token into the third concept.
    concepts[2]["prompt"] = (
        f"{style_lock['positive_fragment']}. Bright anime cel shading "
        "across the price tags."
    )
    return Case[dict[str, Any], dict[str, Any]](
        name="style_lock_violation",
        session_id="coherence-evaluator-case-002",
        input=_case_input(
            concepts,
            style_lock,
            analysis,
            scenes=_scenes_for(analysis),
            whisperx_alignment=_alignment_for(analysis),
        ),
        expected_trajectory=[
            "score_visual_coherence",
            "persist_coherence_report",
        ],
        metadata={
            "expected_rating": "POOR",
            "expected_passed": False,
            "expected_hard_violations_at_least": 1,
            "forbidden_token_in_prompt": "anime",
        },
    )


def _repetitive_shots_case() -> Case[dict[str, Any], dict[str, Any]]:
    analysis = _five_scene_analysis()
    phrases = [
        entry["phrases"][0] for entry in analysis["per_scene"]
    ]
    # Four consecutive identical (shot_type, camera_movement) pairs.
    shots = ["medium", "medium", "medium", "medium", "wide"]
    moves = ["dolly_in", "dolly_in", "dolly_in", "dolly_in", "orbit"]
    concepts = [
        _concept(p, shot_type=s, camera_movement=m)
        for p, s, m in zip(phrases, shots, moves, strict=True)
    ]
    return Case[dict[str, Any], dict[str, Any]](
        name="repetitive_shots",
        session_id="coherence-evaluator-case-003",
        input=_case_input(
            concepts,
            _style_lock(),
            analysis,
            scenes=_scenes_for(analysis),
            whisperx_alignment=_alignment_for(analysis),
        ),
        expected_trajectory=[
            "score_visual_coherence",
            "persist_coherence_report",
        ],
        metadata={
            "expected_rating": "POOR",
            "expected_passed": False,
            "expected_hard_violations_at_least": 1,
            "expected_repetitive_run_length": 4,
        },
    )


def _missing_visual_case() -> Case[dict[str, Any], dict[str, Any]]:
    analysis = _five_scene_analysis()
    phrases = [
        entry["phrases"][0] for entry in analysis["per_scene"]
    ]
    shots = ["wide", "medium", "insert", "medium_close_up", "wide"]
    moves = ["locked", "dolly_in", "graphic_overlay", "handheld", "orbit"]
    concepts = [
        _concept(p, shot_type=s, camera_movement=m)
        for p, s, m in zip(phrases, shots, moves, strict=True)
    ]
    # Drop the fourth concept so phrase 4 is orphaned.
    missing_phrase_id = concepts[3]["phrase_id"]
    del concepts[3]
    return Case[dict[str, Any], dict[str, Any]](
        name="missing_visual",
        session_id="coherence-evaluator-case-004",
        input=_case_input(
            concepts,
            _style_lock(),
            analysis,
            scenes=_scenes_for(analysis),
            whisperx_alignment=_alignment_for(analysis),
        ),
        expected_trajectory=[
            "score_visual_coherence",
            "persist_coherence_report",
        ],
        metadata={
            "expected_rating": "POOR",
            "expected_passed": False,
            "expected_hard_violations_at_least": 1,
            "missing_phrase_id": missing_phrase_id,
        },
    )


def _minor_palette_drift_case() -> Case[dict[str, Any], dict[str, Any]]:
    """One concept drifts off-palette but breaks no hard invariant.

    The soft scorer is expected to call this FAIR because the drift
    is a palette / tone issue (not a forbidden-style token match, not
    a missing visual, not a repetitive run). The deterministic
    structural check returns no hard violations.
    """
    analysis = _five_scene_analysis()
    phrases = [
        entry["phrases"][0] for entry in analysis["per_scene"]
    ]
    shots = ["wide", "medium", "insert", "medium_close_up", "wide"]
    moves = ["locked", "dolly_in", "graphic_overlay", "handheld", "orbit"]
    concepts = [
        _concept(p, shot_type=s, camera_movement=m)
        for p, s, m in zip(phrases, shots, moves, strict=True)
    ]
    # Drift: the third concept loses the warm tungsten tone for cool
    # neon, without mentioning any forbidden style token.
    concepts[2]["prompt"] = (
        f"{_style_lock()['positive_fragment']}. Cold neon-blue accents "
        "wash over the price tags, tilting the palette away from the "
        "documentary's warm tungsten baseline."
    )
    return Case[dict[str, Any], dict[str, Any]](
        name="minor_palette_drift",
        session_id="coherence-evaluator-case-005",
        input=_case_input(
            concepts,
            _style_lock(),
            analysis,
            scenes=_scenes_for(analysis),
            whisperx_alignment=_alignment_for(analysis),
        ),
        expected_trajectory=[
            "score_visual_coherence",
            "persist_coherence_report",
        ],
        metadata={
            "expected_rating_in": ["FAIR", "GOOD"],
            "expected_passed_in": [False, True],
            "expected_hard_violations": 0,
            "palette_drift_phrase_index": 2,
        },
    )


def coherence_evaluator_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the five canonical coherence-evaluator test cases."""
    return [
        _clean_concepts_case(),
        _style_lock_violation_case(),
        _repetitive_shots_case(),
        _missing_visual_case(),
        _minor_palette_drift_case(),
    ]


def coherence_evaluator_evaluators() -> list[
    Evaluator[dict[str, Any], dict[str, Any]]
]:
    """Return the evaluator stack applied to every coherence-evaluator case.

    Hard gates (contract) first, soft gates (output rubric, visual
    coherence judge) after. Order is cosmetic — the experiment runner
    evaluates independently.
    """
    return [
        ContractComplianceEvaluator(VISUAL_DIRECTION_CONTRACT),
        OutputEvaluator(rubric=COHERENCE_REPORT_RUBRIC),
        VisualCoherenceEvaluator(),
    ]


def build_coherence_evaluator_experiment() -> Experiment[
    dict[str, Any], dict[str, Any]
]:
    """Construct the :class:`Experiment` for Component 08."""
    return Experiment(
        cases=coherence_evaluator_cases(),
        evaluators=coherence_evaluator_evaluators(),
    )


__all__ = [
    "COHERENCE_EVALUATOR_THRESHOLDS",
    "COHERENCE_REPORT_RUBRIC",
    "COHERENCE_TOOL_DESCRIPTIONS",
    "COHERENCE_TRAJECTORY_RUBRIC",
    "build_coherence_evaluator_experiment",
    "coherence_evaluator_cases",
    "coherence_evaluator_evaluators",
]
