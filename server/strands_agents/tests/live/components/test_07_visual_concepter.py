"""Live-judge proof of robustness for Component 07 (visual-concepter).

Clear-cut contracts proved here:

1. Deterministic: ``check_style_lock`` catches the blatant style-lock
   failures — prompt missing the ``positive_fragment``, prompt
   containing a forbidden style token, bad shot_type enum, repeated
   camera movement inside the same scene.
2. Live: a Claude-backed concept proposer produces a real visual
   concept; ``check_style_lock`` accepts it; Gemini independently
   judges that the concept prompt actually depicts the phrase the
   narration is about (on-topic, non-hallucinated).
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from strands_agents.visual_concepter import (
    check_style_lock,
    clear_visual_concepter_helpers,
    propose_concept,
    set_visual_concepter_helpers,
)

from .._judges import judge_text_yes, live_claude_text
from ..conftest import requires_anthropic_api, requires_google_api


_TOPIC = "the 1923 Weimar hyperinflation"
_STYLE_LOCK = {
    "dominant_style": "archival-documentary",
    "positive_fragment": "grainy 16mm archival footage",
    "forbidden_styles": ["cyberpunk", "anime", "photorealistic 3d render"],
}
_VISUAL_STYLE = {
    "palette": "sepia and muted grey",
    "avoid": ["neon", "high saturation"],
}


# ---------------------------------------------------------------------------
# Deterministic: structural + style-lock violations
# ---------------------------------------------------------------------------


def _base_concept(**overrides: Any) -> dict[str, Any]:
    concept = {
        "phrase_id": "ph-01-00-abc",
        "scene_id": 1,
        "shot_type": "medium",
        "camera_movement": "dolly_in",
        "prompt": (
            "A weary baker stacks loaves on a wooden counter, "
            "grainy 16mm archival footage, sepia tones, muted grey sky."
        ),
        "negative_prompt": "neon, high saturation, cyberpunk, text, watermark",
        "duration_sec": 4.0,
        "ltx_params": {"resolution": [1280, 720], "seed": None, "steps": 30},
        "style_lock_applied": True,
    }
    concept.update(overrides)
    return concept


def test_check_style_lock_flags_missing_positive_fragment() -> None:
    concept = _base_concept(
        prompt="A baker stacks loaves.  No fragment here.",
    )
    result = check_style_lock.__wrapped__(concepts=[concept], style_lock=_STYLE_LOCK)
    codes = {v["code"] for v in result["violations"]}
    assert not result["ok"]
    assert "missing_positive_fragment" in codes, (
        f"missing_positive_fragment not flagged in {codes}"
    )


def test_check_style_lock_flags_forbidden_token() -> None:
    concept = _base_concept(
        prompt=("A cyberpunk skyline of neon towers, grainy 16mm archival footage."),
    )
    result = check_style_lock.__wrapped__(concepts=[concept], style_lock=_STYLE_LOCK)
    codes = {v["code"] for v in result["violations"]}
    assert not result["ok"]
    assert "forbidden_style_in_prompt" in codes


def test_check_style_lock_flags_bad_shot_type_and_repeated_movement() -> None:
    c1 = _base_concept(shot_type="bokeh_dream", camera_movement="dolly_in")
    c2 = _base_concept(
        phrase_id="ph-01-01-def",
        scene_id=1,
        camera_movement="dolly_in",
    )
    result = check_style_lock.__wrapped__(concepts=[c1, c2], style_lock=_STYLE_LOCK)
    codes = {v["code"] for v in result["violations"]}
    assert not result["ok"]
    assert "bad_shot_type" in codes
    assert "repeated_camera_movement" in codes


def test_check_style_lock_accepts_well_formed_concepts() -> None:
    c1 = _base_concept()
    c2 = _base_concept(
        phrase_id="ph-01-01-def",
        scene_id=1,
        camera_movement="pan_right",
    )
    result = check_style_lock.__wrapped__(concepts=[c1, c2], style_lock=_STYLE_LOCK)
    assert result["ok"], f"well-formed concepts rejected: {result['violations']}"


# ---------------------------------------------------------------------------
# Live: Claude proposes a concept; Gemini judges on-topic depiction
# ---------------------------------------------------------------------------


_PROPOSER_SYSTEM = (
    "You design ONE cinematographic shot for a single documentary "
    "phrase.  Return ONLY a JSON object — no preamble, no markdown "
    "fences, no prose commentary.  The JSON object MUST contain: "
    "shot_type (one of: extreme_close_up, close_up, medium_close_up, "
    "medium, medium_wide, wide, extreme_wide, establishing, detail, "
    "macro, aerial, over_shoulder, two_shot, cutaway, insert), "
    "camera_movement (one of: locked, tripod_locked, dolly_in, "
    "dolly_out, crane_up, crane_down, pan_left, pan_right, "
    "truck_left, truck_right, orbit, handheld, graphic_overlay), "
    "prompt (a 4-6 sentence cinematography paragraph — MUST include "
    "the style_lock.positive_fragment verbatim), negative_prompt "
    "(comma-separated), duration_sec (a number)."
)


def _claude_concept_proposer(
    phrase: dict[str, Any],
    style_lock: dict[str, Any],
    visual_style: dict[str, Any],
) -> dict[str, Any]:
    user = (
        f"Phrase text: {phrase.get('text')!r}\n"
        f"Phrase type: {phrase.get('phrase_type')}\n"
        f"Visual intent: {phrase.get('visual_intent')}\n"
        f"Scene: {phrase.get('scene_num') or phrase.get('scene_id')}\n"
        f"Topic of the documentary: {_TOPIC}\n"
        f"style_lock.positive_fragment (MUST appear verbatim in prompt): "
        f"{style_lock.get('positive_fragment')!r}\n"
        f"style_lock.forbidden_styles: {style_lock.get('forbidden_styles')}\n"
        f"visual_style.palette: {visual_style.get('palette')!r}\n"
        f"visual_style.avoid: {visual_style.get('avoid')}\n\n"
        "Emit the JSON object now."
    )
    raw = live_claude_text(user, system=_PROPOSER_SYSTEM).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    # Take the outermost balanced object.
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        raw = match.group(0)
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture()
def _helpers() -> None:
    clear_visual_concepter_helpers()
    set_visual_concepter_helpers(concept_proposer=_claude_concept_proposer)
    yield
    clear_visual_concepter_helpers()


@requires_anthropic_api
@requires_google_api
def test_claude_concept_honours_style_lock_and_stays_on_topic(
    _helpers: None,
) -> None:
    phrase = {
        "phrase_id": "ph-01-00-abc1234567",
        "scene_id": 1,
        "scene_num": 1,
        "text": "families carried wheelbarrows of banknotes to buy bread",
        "phrase_type": "entity",
        "narrative_weight": "build",
        "visual_intent": (
            "A family pushing an overloaded wheelbarrow of devalued "
            "cash down a cobblestone street toward a bakery."
        ),
        "time_span": [0.0, 4.5],
    }
    concept = propose_concept.__wrapped__(
        phrase=phrase,
        style_lock=_STYLE_LOCK,
        visual_style=_VISUAL_STYLE,
    )

    # The style-lock checker is the deterministic gate.  Claude's
    # concept must pass it without test-side fixups.
    check = check_style_lock.__wrapped__(concepts=[concept], style_lock=_STYLE_LOCK)
    assert check["ok"], (
        f"Claude concept failed the deterministic style-lock gate: "
        f"{check['violations']}"
    )

    # Gemini judges the semantic contract: the prompt actually depicts
    # the phrase.  Clear-cut: if the model generated a scene of a
    # rocket launch for a wheelbarrow phrase, the judge should say no.
    judge_prompt = (
        "You are reviewing a one-shot cinematography paragraph.  Does "
        "the shot described below visually depict the phrase "
        f"'{phrase['text']}' in the context of {_TOPIC}? Answer with "
        "a single word: yes or no.\n\n"
        f"Shot paragraph:\n---\n{concept['prompt']}\n---"
    )
    verdict = judge_text_yes(judge_prompt)
    assert not verdict.disabled, f"judge disabled: {verdict.error}"
    assert verdict.is_yes, (
        f"judge said concept does not depict phrase: "
        f"answer={verdict.answer!r} | concept.prompt={concept['prompt']!r}"
    )
