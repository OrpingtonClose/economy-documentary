"""Visual-concepter experiment factory.

Assembles the :class:`Experiment` that the strands-evals runner
consumes for ``docs/strands-migration/components/07-visual-concepter.md``.
Five cases covering cinematic documentary, hand-drawn animation,
realism anchors, a forbidden-style retry case, and a data-heavy
phrase. Four-evaluator stack mirroring the THRESHOLDS table.

The ``task`` callable supplied to :meth:`Experiment.run_evaluations`
is provided by whoever drives the run (CI, shadow runner, notebook)
so this module stays free of LLM calls and can be assembled
deterministically inside pytest.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case  # type: ignore[import-not-found]
from strands_evals.evaluators.coherence_evaluator import CoherenceEvaluator  # type: ignore[import-not-found]
from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.evaluators.tool_selection_accuracy_evaluator import (  # type: ignore[import-not-found]
    ToolSelectionAccuracyEvaluator,
)
from strands_evals.experiment import Experiment  # type: ignore[import-not-found]

from contracts import VISUAL_DIRECTION_CONTRACT
from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
    VisualCoherenceEvaluator,
)


#: Minimum score per evaluator — mirrors ``eval-framework/THRESHOLDS.md``.
#: ``True`` in the second tuple position indicates a hard gate.
VISUAL_CONCEPTER_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "ContractComplianceEvaluator": (1.0, True),
    "VisualCoherenceEvaluator": (0.7, False),
    "ToolSelectionAccuracyEvaluator": (0.7, False),
    "CoherenceEvaluator": (0.7, False),
}


#: Tool descriptions surfaced to the judge LLM used by
#: :class:`ToolSelectionAccuracyEvaluator`.
VISUAL_CONCEPTER_TOOL_DESCRIPTIONS = {
    "propose_concept": (
        "Generate one LTX-2.3 visual concept for one phrase. Requires the "
        "phrase dict, the movie-level style_lock, and the visual_style. "
        "Must be called once per phrase in content_analysis order."
    ),
    "check_style_lock": (
        "Structural check that every concept carries style_lock."
        "positive_fragment, avoids forbidden_styles, and uses shot_type + "
        "camera_movement from the closed vocabulary. Call once on the "
        "accumulated concepts before persistence."
    ),
    "persist_visual_concepts": (
        "Commit the final visual_concepts list onto agent.state. Terminal "
        "tool — no further tools should be invoked after this."
    ),
}


def _phrase(
    scene_id: int,
    phrase_idx: int,
    *,
    text: str,
    phrase_type: str = "concept",
    narrative_weight: str = "build",
    time_span: tuple[float, float] = (0.0, 3.0),
) -> dict[str, Any]:
    return {
        "phrase_id": f"ph-{scene_id:02d}-{phrase_idx:02d}-{abs(hash(text)) % (10**10):010d}",
        "scene_id": scene_id,
        "scene_num": scene_id,
        "phrase_type": phrase_type,
        "narrative_weight": narrative_weight,
        "visual_intent": text,
        "text": text,
        "word_span": [0, len(text.split())],
        "time_span": list(time_span),
    }


def _scene_entry(scene_id: int, phrases: list[dict[str, Any]]) -> dict[str, Any]:
    return {"scene_num": scene_id, "phrases": phrases}


def _content_analysis(scene_entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"per_scene": scene_entries}


def _cinematic_style_lock() -> dict[str, Any]:
    return {
        "dominant_style": "cinematic_documentary",
        "positive_fragment": "shot on 35mm film with shallow depth of field",
        "negative_fragment": "cartoon, anime, illustration",
        "forbidden_styles": ["anime", "cartoon", "cyberpunk", "vaporwave"],
        "palette": ["warm tungsten", "soft daylight"],
        "realism_anchors": ["4K", "no CGI"],
    }


def _hand_drawn_style_lock() -> dict[str, Any]:
    return {
        "dominant_style": "hand_drawn_animation",
        "positive_fragment": "hand-drawn 2D animation with visible ink linework",
        "negative_fragment": "photo real, 3D, CGI",
        "forbidden_styles": ["realistic_3d", "photograph", "cgi"],
        "palette": ["ink black", "sepia wash"],
        "realism_anchors": [],
    }


def _standard_visual_style() -> dict[str, Any]:
    return {
        "style": "cinematic documentary",
        "tone": "measured, observational",
        "avoid": ["stock footage cliches", "cheesy transitions"],
    }


def _case_input(
    content_analysis: dict[str, Any],
    style_lock: dict[str, Any],
    visual_style: dict[str, Any],
) -> dict[str, Any]:
    return {
        "content_analysis": content_analysis,
        "style_lock": style_lock,
        "visual_style": visual_style,
        "scenes": [
            {"scene_num": entry["scene_num"], "id": entry["scene_num"]}
            for entry in content_analysis.get("per_scene", [])
        ],
        "whisperx_alignment": [
            {
                "scene_num": entry["scene_num"],
                "start": 0.0,
                "end": 30.0,
                "text": f"scene-{entry['scene_num']} alignment",
            }
            for entry in content_analysis.get("per_scene", [])
        ],
    }


def _cinematic_doc_case() -> Case[dict[str, Any], dict[str, Any]]:
    analysis = _content_analysis(
        [
            _scene_entry(
                1,
                [
                    _phrase(
                        1,
                        0,
                        text="Open on a quiet trading floor at dawn.",
                        phrase_type="concept",
                        narrative_weight="hook",
                        time_span=(0.0, 4.0),
                    ),
                    _phrase(
                        1,
                        1,
                        text="Tickers hum as the first orders arrive.",
                        phrase_type="process",
                        narrative_weight="build",
                        time_span=(4.0, 8.0),
                    ),
                ],
            ),
            _scene_entry(
                2,
                [
                    _phrase(
                        2,
                        0,
                        text="Central bankers meet behind closed doors.",
                        phrase_type="entity",
                        narrative_weight="build",
                        time_span=(8.0, 12.0),
                    ),
                    _phrase(
                        2,
                        1,
                        text="Rate decisions ripple through every market.",
                        phrase_type="process",
                        narrative_weight="payoff",
                        time_span=(12.0, 16.0),
                    ),
                ],
            ),
        ]
    )
    return Case[dict[str, Any], dict[str, Any]](
        name="cinematic_doc",
        session_id="visual-concepter-case-001",
        input=_case_input(
            analysis, _cinematic_style_lock(), _standard_visual_style()
        ),
        expected_trajectory=[
            "propose_concept",
            "propose_concept",
            "propose_concept",
            "propose_concept",
            "check_style_lock",
            "persist_visual_concepts",
        ],
        metadata={
            "expected_concept_count": 4,
            "expected_dominant_style": "cinematic_documentary",
            "forbidden_in_prompt": ["anime", "cartoon"],
        },
    )


def _hand_drawn_case() -> Case[dict[str, Any], dict[str, Any]]:
    analysis = _content_analysis(
        [
            _scene_entry(
                1,
                [
                    _phrase(
                        1,
                        0,
                        text="A single drawn line sweeps across the page.",
                        phrase_type="concept",
                        narrative_weight="hook",
                        time_span=(0.0, 3.5),
                    )
                ],
            ),
            _scene_entry(
                2,
                [
                    _phrase(
                        2,
                        0,
                        text="Ink pools where the artist hesitates.",
                        phrase_type="process",
                        narrative_weight="build",
                        time_span=(3.5, 7.0),
                    )
                ],
            ),
            _scene_entry(
                3,
                [
                    _phrase(
                        3,
                        0,
                        text="The final frame closes with a simple iris.",
                        phrase_type="transition",
                        narrative_weight="payoff",
                        time_span=(7.0, 10.5),
                    )
                ],
            ),
        ]
    )
    return Case[dict[str, Any], dict[str, Any]](
        name="hand_drawn",
        session_id="visual-concepter-case-002",
        input=_case_input(
            analysis, _hand_drawn_style_lock(), _standard_visual_style()
        ),
        expected_trajectory=[
            "propose_concept",
            "propose_concept",
            "propose_concept",
            "check_style_lock",
            "persist_visual_concepts",
        ],
        metadata={
            "expected_concept_count": 3,
            "expected_dominant_style": "hand_drawn_animation",
            "required_in_prompt": ["2D", "ink"],
            "forbidden_in_prompt": ["3D", "photograph"],
        },
    )


def _realism_anchor_case() -> Case[dict[str, Any], dict[str, Any]]:
    analysis = _content_analysis(
        [
            _scene_entry(
                1,
                [
                    _phrase(
                        1,
                        0,
                        text="A factory floor observed through 4K documentary lenses.",
                        phrase_type="entity",
                        narrative_weight="hook",
                        time_span=(0.0, 4.0),
                    )
                ],
            ),
            _scene_entry(
                2,
                [
                    _phrase(
                        2,
                        0,
                        text="Machines breathe steam as shifts change.",
                        phrase_type="process",
                        narrative_weight="payoff",
                        time_span=(4.0, 9.0),
                    )
                ],
            ),
        ]
    )
    style_lock = _cinematic_style_lock()
    style_lock["realism_anchors"] = ["4K", "no CGI"]
    return Case[dict[str, Any], dict[str, Any]](
        name="realism_anchor",
        session_id="visual-concepter-case-003",
        input=_case_input(analysis, style_lock, _standard_visual_style()),
        expected_trajectory=[
            "propose_concept",
            "propose_concept",
            "check_style_lock",
            "persist_visual_concepts",
        ],
        metadata={
            "expected_concept_count": 2,
            "required_in_negative_prompt": ["CGI", "cartoon"],
        },
    )


def _forbidden_style_case() -> Case[dict[str, Any], dict[str, Any]]:
    analysis = _content_analysis(
        [
            _scene_entry(
                1,
                [
                    _phrase(
                        1,
                        0,
                        text="Shadows spread across a silent newsroom.",
                        phrase_type="concept",
                        narrative_weight="hook",
                        time_span=(0.0, 3.5),
                    )
                ],
            ),
            _scene_entry(
                2,
                [
                    _phrase(
                        2,
                        0,
                        text="A reporter's hand hovers over a trembling microphone.",
                        phrase_type="entity",
                        narrative_weight="payoff",
                        time_span=(3.5, 7.5),
                    )
                ],
            ),
        ]
    )
    style_lock = _cinematic_style_lock()
    style_lock["forbidden_styles"] = ["anime", "manga", "chibi"]
    return Case[dict[str, Any], dict[str, Any]](
        name="forbidden_style",
        session_id="visual-concepter-case-004",
        input=_case_input(analysis, style_lock, _standard_visual_style()),
        expected_trajectory=[
            "propose_concept",
            "propose_concept",
            "propose_concept",
            "check_style_lock",
            "persist_visual_concepts",
        ],
        metadata={
            "expected_concept_count": 2,
            "expect_style_lock_retry": True,
            "forbidden_in_prompt": ["anime", "manga", "chibi"],
        },
    )


def _phrase_data_heavy_case() -> Case[dict[str, Any], dict[str, Any]]:
    analysis = _content_analysis(
        [
            _scene_entry(
                1,
                [
                    _phrase(
                        1,
                        0,
                        text="A quiet intro before the numbers arrive.",
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
                        text="Headline CPI rose 3.4% year-over-year in Q2.",
                        phrase_type="data",
                        narrative_weight="build",
                        time_span=(3.0, 7.0),
                    ),
                    _phrase(
                        2,
                        1,
                        text="Core CPI held at 3.6% while PPI lagged at 2.1%.",
                        phrase_type="data",
                        narrative_weight="payoff",
                        time_span=(7.0, 12.0),
                    ),
                ],
            ),
        ]
    )
    return Case[dict[str, Any], dict[str, Any]](
        name="phrase_data_heavy",
        session_id="visual-concepter-case-005",
        input=_case_input(
            analysis, _cinematic_style_lock(), _standard_visual_style()
        ),
        expected_trajectory=[
            "propose_concept",
            "propose_concept",
            "propose_concept",
            "check_style_lock",
            "persist_visual_concepts",
        ],
        metadata={
            "expected_concept_count": 3,
            "data_phrase_shot_types": [
                "insert",
                "cutaway",
                "detail",
                "macro",
                "aerial",
            ],
        },
    )


def visual_concepter_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the five canonical visual-concepter test cases."""
    return [
        _cinematic_doc_case(),
        _hand_drawn_case(),
        _realism_anchor_case(),
        _forbidden_style_case(),
        _phrase_data_heavy_case(),
    ]


def visual_concepter_evaluators() -> list[
    Evaluator[dict[str, Any], dict[str, Any]]
]:
    """Return the evaluator stack applied to every visual-concepter case.

    Hard gates (contract) first, soft gates (coherence, tool selection,
    visual coherence) after. Order is cosmetic — the experiment runner
    evaluates independently.
    """
    return [
        ContractComplianceEvaluator(VISUAL_DIRECTION_CONTRACT),
        VisualCoherenceEvaluator(),
        ToolSelectionAccuracyEvaluator(),
        CoherenceEvaluator(),
    ]


def build_visual_concepter_experiment() -> Experiment[
    dict[str, Any], dict[str, Any]
]:
    """Construct the :class:`Experiment` for Component 07."""
    return Experiment(
        cases=visual_concepter_cases(),
        evaluators=visual_concepter_evaluators(),
    )


def visual_concepter_task(case: Case) -> dict[str, Any]:
    """Replay task adapter for the component-playground surface.

    Returns the case's canonical envelope so the evaluate endpoint can
    score a known-good payload against this component's evaluator
    stack without a live agent run. A live runner can replace this
    once provider plumbing lands in the playground.
    """
    metadata = case.metadata or {}
    expected_output: Any = (
        case.expected_output if case.expected_output is not None else {}
    )
    trajectory = case.expected_trajectory
    if trajectory is None:
        trajectory = metadata.get("canonical_trajectory")
    if trajectory is None:
        trajectory = []
    return {
        "output": expected_output,
        "trajectory": list(trajectory),
        "metadata": {"mode": "replay", "case": case.name},
    }


__all__ = ["VISUAL_CONCEPTER_TOOL_DESCRIPTIONS",
    "build_visual_concepter_experiment",
    "visual_concepter_cases",
    "visual_concepter_evaluators",
    "visual_concepter_task",]
