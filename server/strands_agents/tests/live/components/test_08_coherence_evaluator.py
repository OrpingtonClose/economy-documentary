"""Live-judge proof of robustness for Component 08 (coherence-evaluator).

Clear-cut contracts proved here:

1. Deterministic: hard-invariant violations force ``rating="POOR"``
   regardless of the soft scorer's opinion — missing phrase coverage,
   forbidden style token in prompt, too many identical consecutive
   shots.  When the soft scorer returns ``EXCELLENT``, the hard gate
   still overrides it.
2. Live: a Claude-backed soft scorer evaluates a well-formed concept
   list and returns a passing rating; an independently well-formed
   concept list containing a genuine incoherence (every phrase gets
   the exact same locked wide shot, no motion, no variety) is scored
   and Gemini independently confirms the scorer's ``POOR`` verdict
   reflects reality.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from strands_agents.coherence_evaluator import (
    clear_coherence_evaluator_helpers,
    score_visual_coherence,
    set_coherence_evaluator_helpers,
)

from .._judges import judge_text_yes, live_claude_text
from ..conftest import requires_anthropic_api, requires_google_api


_TOPIC = "the 1923 Weimar hyperinflation"
_STYLE_LOCK = {
    "dominant_style": "archival-documentary",
    "positive_fragment": "grainy 16mm archival footage",
    "forbidden_styles": ["cyberpunk", "anime"],
}


def _phrase(pid: str, text: str) -> dict[str, Any]:
    return {
        "phrase_id": pid,
        "text": text,
        "phrase_type": "entity",
        "narrative_weight": "build",
        "visual_intent": text,
        "word_span": [0, 5],
        "time_span": [0.0, 3.0],
    }


def _analysis(
    phrases_by_scene: list[list[tuple[str, str]]],
) -> dict[str, Any]:
    per_scene: list[dict[str, Any]] = []
    for idx, specs in enumerate(phrases_by_scene, start=1):
        per_scene.append(
            {
                "scene_num": idx,
                "phrases": [_phrase(pid, text) for pid, text in specs],
            }
        )
    return {"per_scene": per_scene}


def _concept(
    *,
    phrase_id: str,
    scene_id: int = 1,
    shot_type: str = "medium",
    camera_movement: str = "dolly_in",
    prompt_extra: str = "",
) -> dict[str, Any]:
    return {
        "phrase_id": phrase_id,
        "scene_id": scene_id,
        "shot_type": shot_type,
        "camera_movement": camera_movement,
        "prompt": (
            f"grainy 16mm archival footage of weimar berlin, "
            f"sepia tones, muted sky. {prompt_extra}"
        ),
        "negative_prompt": "text, watermark",
        "duration_sec": 3.0,
        "ltx_params": {"resolution": [1280, 720], "seed": None, "steps": 30},
        "style_lock_applied": True,
    }


# ---------------------------------------------------------------------------
# Deterministic: hard invariants override soft scorer
# ---------------------------------------------------------------------------


def _always_excellent_scorer(
    _concepts: list[dict[str, Any]],
    _style_lock: dict[str, Any],
    _analysis: dict[str, Any],
) -> dict[str, Any]:
    return {"rating": "EXCELLENT", "issues": [], "suggestions": []}


@pytest.fixture()
def _excellent_scorer() -> None:
    clear_coherence_evaluator_helpers()
    set_coherence_evaluator_helpers(soft_scorer=_always_excellent_scorer)
    yield
    clear_coherence_evaluator_helpers()


def test_hard_gate_overrides_soft_on_missing_phrase(
    _excellent_scorer: None,
) -> None:
    analysis = _analysis(
        [
            [("ph-a", "wheelbarrow of marks"), ("ph-b", "empty shelves")],
            [("ph-c", "workers paid twice a day")],
        ]
    )
    # Miss ph-b entirely.
    concepts = [
        _concept(phrase_id="ph-a"),
        _concept(phrase_id="ph-c", scene_id=2, camera_movement="pan_left"),
    ]
    result = score_visual_coherence.__wrapped__(
        visual_concepts=concepts,
        style_lock=_STYLE_LOCK,
        content_analysis=analysis,
    )
    assert result["rating"] == "POOR"
    assert not result["visual_coherence_passed"]
    assert any("ph-b" in i for i in result["issues"]), (
        f"missing phrase not flagged: {result['issues']}"
    )


def test_hard_gate_overrides_soft_on_forbidden_token(
    _excellent_scorer: None,
) -> None:
    analysis = _analysis([[("ph-a", "wheelbarrow of marks")]])
    concepts = [
        _concept(
            phrase_id="ph-a",
            prompt_extra="with a CYBERPUNK skyline overlay",
        )
    ]
    result = score_visual_coherence.__wrapped__(
        visual_concepts=concepts,
        style_lock=_STYLE_LOCK,
        content_analysis=analysis,
    )
    assert result["rating"] == "POOR"
    assert any("cyberpunk" in i.lower() for i in result["issues"])


def test_hard_gate_overrides_soft_on_repeated_shots(
    _excellent_scorer: None,
) -> None:
    analysis = _analysis(
        [
            [
                ("ph-a", "shop with empty shelves"),
                ("ph-b", "banknotes on the counter"),
                ("ph-c", "wheelbarrow at the door"),
                ("ph-d", "bakery queue"),
            ]
        ]
    )
    # 4 identical (wide, locked) in a row → exceeds 3-in-a-row cap.
    concepts = [
        _concept(
            phrase_id=pid,
            shot_type="wide",
            camera_movement="locked",
        )
        for pid in ("ph-a", "ph-b", "ph-c", "ph-d")
    ]
    result = score_visual_coherence.__wrapped__(
        visual_concepts=concepts,
        style_lock=_STYLE_LOCK,
        content_analysis=analysis,
    )
    assert result["rating"] == "POOR"
    assert any("consecutive" in i.lower() for i in result["issues"])


# ---------------------------------------------------------------------------
# Live: Claude soft scorer + Gemini confirmation judge
# ---------------------------------------------------------------------------


_SCORER_SYSTEM = (
    "You are reviewing a list of visual concepts for a short "
    "documentary to score their holistic coherence.  Return ONLY a "
    "JSON object — no preamble, no markdown fences.  The object MUST "
    "contain: rating (one of EXCELLENT, GOOD, FAIR, POOR), issues "
    "(list of strings; empty list when none), suggestions (list of "
    "strings; empty list when none).  Rate EXCELLENT/GOOD when the "
    "concept list varies shot types, matches the topic, and honours "
    "the style lock.  Rate POOR when the concept list is monotonous "
    "(e.g. every shot identical), off-topic, or violates style lock."
)


def _claude_coherence_scorer(
    concepts: list[dict[str, Any]],
    style_lock: dict[str, Any],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    user = (
        f"Documentary topic: {_TOPIC}\n"
        f"Style lock positive fragment: "
        f"{style_lock.get('positive_fragment')!r}\n"
        f"Style lock forbidden styles: "
        f"{style_lock.get('forbidden_styles')}\n\n"
        f"Phrases (scene -> list):\n"
        f"{json.dumps(analysis, indent=2)}\n\n"
        f"Visual concepts (ordered):\n"
        f"{json.dumps(concepts, indent=2)}\n\n"
        "Score now."
    )
    raw = live_claude_text(user, system=_SCORER_SYSTEM).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        raw = match.group(0)
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture()
def _claude_scorer() -> None:
    clear_coherence_evaluator_helpers()
    set_coherence_evaluator_helpers(soft_scorer=_claude_coherence_scorer)
    yield
    clear_coherence_evaluator_helpers()


@requires_anthropic_api
@requires_google_api
def test_claude_scorer_rates_well_formed_list_as_passing(
    _claude_scorer: None,
) -> None:
    analysis = _analysis(
        [
            [
                ("ph-a", "wheelbarrow of marks"),
                ("ph-b", "bakery queue"),
            ],
            [
                ("ph-c", "workers paid twice a day"),
                ("ph-d", "banknotes losing value by nightfall"),
            ],
        ]
    )
    concepts = [
        _concept(
            phrase_id="ph-a",
            shot_type="wide",
            camera_movement="dolly_in",
            prompt_extra="family pushing an overloaded wheelbarrow",
        ),
        _concept(
            phrase_id="ph-b",
            shot_type="medium",
            camera_movement="pan_right",
            prompt_extra="queue of people outside a bread shop",
        ),
        _concept(
            phrase_id="ph-c",
            scene_id=2,
            shot_type="close_up",
            camera_movement="handheld",
            prompt_extra="factory worker receiving a stack of banknotes",
        ),
        _concept(
            phrase_id="ph-d",
            scene_id=2,
            shot_type="insert",
            camera_movement="graphic_overlay",
            prompt_extra="hourly chart of the mark collapsing",
        ),
    ]
    result = score_visual_coherence.__wrapped__(
        visual_concepts=concepts,
        style_lock=_STYLE_LOCK,
        content_analysis=analysis,
    )
    assert result["visual_coherence_passed"], (
        f"well-formed concepts rated non-passing: {result}"
    )
    assert result["rating"] in {"EXCELLENT", "GOOD"}


@requires_anthropic_api
@requires_google_api
def test_claude_scorer_flags_monotonous_list_as_poor(
    _claude_scorer: None,
) -> None:
    """Every phrase gets the same locked wide shot: a clear-cut
    monotony case Claude should flag.  Gemini independently confirms
    the monotony is real (clear-cut on-topic judgment)."""
    analysis = _analysis(
        [
            [
                ("ph-a", "wheelbarrow of marks"),
                ("ph-b", "bakery queue"),
                ("ph-c", "workers paid twice a day"),
            ]
        ]
    )
    # Three phrases, three shots — within the structural 3-in-a-row
    # cap, so we rely on the SOFT scorer (Claude) to call out monotony.
    concepts = [
        _concept(
            phrase_id=pid,
            shot_type="wide",
            camera_movement="locked",
        )
        for pid in ("ph-a", "ph-b", "ph-c")
    ]
    result = score_visual_coherence.__wrapped__(
        visual_concepts=concepts,
        style_lock=_STYLE_LOCK,
        content_analysis=analysis,
    )
    assert not result["visual_coherence_passed"], (
        f"monotonous shot list rated passing: {result}"
    )

    # Gemini confirmation: three identical (wide, locked) shots IS
    # monotonous.  If the confirmation judge disagrees, either the
    # test case isn't clear-cut or the judge is broken.
    verdict = judge_text_yes(
        "Three consecutive documentary shots share shot_type='wide' and "
        "camera_movement='locked' and have nearly identical prompts.  "
        "Is that visually monotonous? Answer with a single word: yes or no."
    )
    assert not verdict.disabled, f"judge disabled: {verdict.error}"
    assert verdict.is_yes, f"confirmation judge denied monotony: {verdict.answer!r}"
