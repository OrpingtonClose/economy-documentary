"""Live-judge proof of robustness for Component 03 (scenario-refiner).

Clear-cut contracts proved here:

1. Deterministic: ``adjust_scene_durations`` mutates ONLY the target
   field and leaves pronunciation hints, voice IDs, hook_spec and
   outro_spec byte-identical.
2. Live: ``tweak_voice_text`` driven by a real Claude-backed rewriter
   actually shortens narration when asked to shorten it, and the
   shortened narration is judged by a separate model family (Gemini)
   to still be on-topic.  Honoring the refiner feedback is the
   semantic contract.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import pytest

from strands_agents.scenario_refiner import (
    adjust_scene_durations,
    clear_refiner_helpers,
    set_refiner_helpers,
    tweak_voice_text,
)

from .._judges import judge_text_yes, live_claude_text
from ..conftest import requires_anthropic_api, requires_google_api


_TOPIC = "the 1923 Weimar hyperinflation"
_ORIGINAL_LINE = (
    "In late 1923, prices in the Weimar Republic doubled every two days, "
    "and families carried wheelbarrows of marks to buy a single loaf of "
    "bread.  Factory workers were paid twice a day so they could spend "
    "their wages before the banknotes lost half their value by nightfall."
)


# ---------------------------------------------------------------------------
# Deterministic: structure-preservation invariants
# ---------------------------------------------------------------------------


def test_adjust_scene_durations_preserves_structure() -> None:
    scenes = [
        {
            "id": 1,
            "scene_num": 1,
            "title": "intro",
            "target_duration_sec": 45.0,
            "voices": [{"voice_id": "narrator_a", "text": _ORIGINAL_LINE}],
            "pronunciation_hints": [{"token": "Weimar", "phoneme": "VY-mar"}],
            "hook_spec": {
                "topic_specific_motif": "wheelbarrow of marks",
                "motion_description": "dolly in",
                "narrative_pull": "what was it worth?",
            },
        },
        {
            "id": 2,
            "scene_num": 2,
            "title": "peak",
            "target_duration_sec": 45.0,
            "voices": [{"voice_id": "narrator_a", "text": "second scene."}],
            "pronunciation_hints": [],
        },
    ]
    result = adjust_scene_durations.__wrapped__(
        scenes=scenes,
        per_scene_targets={"1": 30.0},
    )
    s1 = result["scenes"][0]
    assert s1["target_duration_sec"] == 30.0
    # All non-target fields must be untouched.
    assert s1["voices"][0]["text"] == _ORIGINAL_LINE
    assert s1["voices"][0]["voice_id"] == "narrator_a"
    assert s1["pronunciation_hints"] == [
        {"token": "Weimar", "phoneme": "VY-mar"}
    ]
    assert s1["hook_spec"]["topic_specific_motif"] == "wheelbarrow of marks"
    # Other scenes untouched.
    assert result["scenes"][1]["target_duration_sec"] == 45.0
    assert result["updated_scene_ids"] == [1]


# ---------------------------------------------------------------------------
# Live: Claude-backed text rewriter
# ---------------------------------------------------------------------------


def _claude_text_rewriter(
    text: str,
    direction: Literal["shorten", "lengthen"],
    delta_sec: float,
) -> str:
    """Real Claude-backed rewriter.  Mirrors the production helper."""
    # 150 wpm → 2.5 words/sec; delta_sec translates to ~2.5 * delta_sec words.
    approx_words = max(1, int(round(delta_sec * 2.5)))
    system = (
        "You rewrite a single documentary narration line.  Return ONLY "
        "the rewritten line, no preamble, no markdown, no commentary.  "
        "Preserve the factual content and the topic.  Do not add "
        "all-caps acronyms."
    )
    user = (
        f"Please {direction} the following line by about {approx_words} "
        f"words (~{delta_sec:.1f} seconds of speech).  Keep it about "
        f"'{_TOPIC}'.\n\n"
        f"Original line:\n---\n{text}\n---\n\nRewritten line:"
    )
    body = live_claude_text(user, system=system).strip()
    # Strip quotes and code fences if the model wrapped its output.
    body = body.strip("`").strip('"').strip("'").strip()
    return body or text


@pytest.fixture()
def _helpers() -> None:
    clear_refiner_helpers()
    set_refiner_helpers(text_rewriter=_claude_text_rewriter)
    yield
    clear_refiner_helpers()


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


@requires_anthropic_api
@requires_google_api
def test_tweak_voice_text_shortens_narration_and_stays_on_topic(
    _helpers: None,
) -> None:
    """The full feedback-honoring contract.

    The refiner is told "shorten this scene's narration by about 6 s"
    (~15 words at 150 wpm).  We then check:

    1. Narration is actually shorter than the original (honored the
       feedback — not a no-op).
    2. Voice ID preserved (structure invariant).
    3. Gemini, a separate model family, confirms the shortened line
       is still about hyperinflation (semantic fidelity preserved).
    """
    scenes = [
        {
            "id": 1,
            "scene_num": 1,
            "target_duration_sec": 45.0,
            "voices": [{"voice_id": "narrator_a", "text": _ORIGINAL_LINE}],
            "pronunciation_hints": [{"token": "Weimar", "phoneme": "VY-mar"}],
        }
    ]
    original_words = _word_count(_ORIGINAL_LINE)

    result = tweak_voice_text.__wrapped__(
        scenes=scenes,
        scene_id=1,
        direction="shorten",
        delta_sec=6.0,
    )
    new_text = result["scenes"][0]["voices"][0]["text"]
    new_words = _word_count(new_text)
    assert new_words < original_words, (
        f"refiner did not shorten: {original_words} -> {new_words} words; "
        f"new text={new_text!r}"
    )
    assert result["scenes"][0]["voices"][0]["voice_id"] == "narrator_a", (
        "voice_id was altered by the rewriter"
    )
    assert result["scenes"][0]["pronunciation_hints"], (
        "pronunciation_hints lost by the rewriter"
    )
    assert result["changed_voice_count"] == 1

    prompt = (
        f"Is the following line still about '{_TOPIC}' (Weimar Germany "
        f"1923, rapid currency devaluation)?\n\nLine:\n---\n{new_text}\n"
        "---\n\nAnswer with a single word: yes or no."
    )
    verdict = judge_text_yes(prompt)
    assert not verdict.disabled, f"judge disabled: {verdict.error}"
    assert verdict.is_yes, (
        f"Gemini judged shortened line as off-topic; line={new_text!r} "
        f"answer={verdict.answer!r}"
    )
