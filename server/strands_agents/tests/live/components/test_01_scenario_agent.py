"""Live-judge proof of robustness for Component 01 (scenario-agent).

What this test proves:

* When driven by a real LLM (Claude) the ``generate_scenario`` tool
  produces scenes that **actually cover the requested topic** in the
  **requested language** — verified by a separate, cross-family LLM
  judge (Gemini) so the generator and judge can't collude.
* The deterministic structural evaluator accepts the Claude-produced
  scenario (hook present, outro present, scene count respected,
  duration budget honoured, style-lock valid).
* A deliberately off-topic scenario is rejected by the same Gemini
  judge — the clear-cut contra-case.

Judge rubric is binary yes/no at temperature 0 with three-run
determinism on the main gate.  Per the "must pass every time" policy,
a flipped verdict here is a judge regression, not flakiness.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from strands_agents.scenario_agent import (
    clear_scenario_helpers,
    evaluate_scenario,
    generate_scenario,
    set_scenario_helpers,
)

from .._judges import (
    judge_text_deterministic_yes,
    judge_text_yes,
    live_claude_text,
)
from ..conftest import requires_anthropic_api, requires_google_api


# ---------------------------------------------------------------------------
# Live Claude-backed generator (wired via set_scenario_helpers).
# ---------------------------------------------------------------------------


_SYSTEM = (
    "You are a senior documentary scenario writer.  Respond with ONLY a "
    "JSON object matching the schema described in the user prompt.  No "
    "markdown code fences, no prose outside the JSON.  Each narration "
    "line must be in the requested language."
)


def _generator_prompt(
    topic: str, num_scenes: int, style: str, language: str
) -> str:
    per_scene = 45.0
    return (
        f"Topic: {topic}\n"
        f"Number of scenes: {num_scenes}\n"
        f"Target duration per scene (seconds): {per_scene}\n"
        f"Visual style: {style}\n"
        f"Narration language: {language}\n"
        "\n"
        "Return JSON in the exact shape:\n"
        "{\n"
        "  \"scenes\": [\n"
        "    {\n"
        "      \"scene_num\": int,\n"
        "      \"title\": str,\n"
        "      \"voices\": [{\"text\": str}],\n"
        "      \"duration_sec\": float,\n"
        "      \"visual_notes\": str,\n"
        "      \"hook_spec\": {...}  // only on scene 1\n"
        "      \"outro_spec\": {...}  // only on the last scene\n"
        "    }, ...\n"
        "  ],\n"
        "  \"visual_style\": {\"dominant_style\": str},\n"
        "  \"style_lock\": {\n"
        "    \"dominant_style\": str,\n"
        "    \"forbidden_styles\": [str, ...],\n"
        "    \"positive_fragment\": str,\n"
        "    \"negative_fragment\": str\n"
        "  }\n"
        "}\n"
        "\n"
        "Rules:\n"
        "- Narration must be substantive and on-topic.  Each scene's "
        "  voices[0].text must contain at least "
        f"{int(per_scene * 1.2 * 150 / 60)} words of narration.\n"
        "- scene 1 MUST include hook_spec with keys "
        "`topic_specific_motif`, `motion_description`, `narrative_pull`.\n"
        "- last scene MUST include outro_spec with keys "
        "`closing_shot`, `recap_sentence`, `cta`, `brand_card`.\n"
        "- style_lock.dominant_style must match visual_style.dominant_style.\n"
        "- Narration text must NOT contain any ALL-CAPS acronyms "
        "(e.g., 'US', 'GDP', 'CPI').  Spell them out as words.  "
        "This is a hard pipeline invariant, not a style preference.\n"
    )


def _claude_scenario_generator(
    topic: str, num_scenes: int, style: str, language: str
) -> dict[str, Any]:
    """Real Claude-backed implementation of the scenario-generator helper.

    Returns the parsed JSON produced by Claude.  If Claude's JSON is
    malformed, the test surfaces that as a hard failure — we want to
    know when the production prompt can no longer coax the production
    model into the expected shape.
    """
    body = live_claude_text(
        _generator_prompt(topic, num_scenes, style, language),
        system=_SYSTEM,
    )
    # Strip accidental ```json fences if the model ignores the instruction.
    stripped = body.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Claude returned non-JSON scenario (first 200 chars): {body[:200]!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_TOPIC = "the 1923 Weimar hyperinflation"
_LANGUAGE = "en-US"
_STYLE = "cinematic_documentary"
_NUM_SCENES = 3
_TARGET_DURATION = _NUM_SCENES * 45.0


@pytest.fixture()
def _helpers() -> None:
    """Wire Claude as the generator helper; tear down on exit."""
    clear_scenario_helpers()
    set_scenario_helpers(generator=_claude_scenario_generator)
    yield
    clear_scenario_helpers()


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------


@requires_anthropic_api
@requires_google_api
def test_claude_generates_on_topic_scenario_that_evaluator_accepts(
    _helpers: None,
) -> None:
    """Full loop: Claude generates → structural pass → Gemini judges on-topic.

    Proves the three core invariants for a fresh scenario:
    1. Claude-generated scenes parse into the component's expected schema.
    2. The deterministic structural evaluator does not hard-fail.
    3. An independent LLM family (Gemini) agrees the narration is on-topic
       and in the requested language — every run, no flips.
    """
    scenario = generate_scenario.__wrapped__(
        topic=_TOPIC,
        num_scenes=_NUM_SCENES,
        style=_STYLE,
        language=_LANGUAGE,
    )
    scenes = scenario["scenes"]
    assert len(scenes) == _NUM_SCENES
    assert "style_lock" in scenario, "style_lock missing from Claude output"
    assert "hook_spec" in scenes[0], "scene 1 missing hook_spec"
    assert "outro_spec" in scenes[-1], "last scene missing outro_spec"

    verdict = evaluate_scenario.__wrapped__(
        scenes=scenes,
        style_lock=scenario["style_lock"],
        target_duration_sec=_TARGET_DURATION,
    )
    hard_fails = [i for i in verdict["issues"] if i.get("verdict_cap") == "POOR"]
    assert hard_fails == [], (
        f"Claude-generated scenario hard-failed structural checks: "
        f"{hard_fails}"
    )
    assert verdict["rating"] in {"EXCELLENT", "GOOD", "FAIR"}, (
        f"unexpected rating: {verdict['rating']} issues={verdict['issues']}"
    )

    # Concatenate all narration for the semantic judge.
    narration = " ".join(
        voice.get("text", "")
        for scene in scenes
        for voice in scene.get("voices", [])
    )
    assert narration, "no narration text produced by Claude"

    prompt = (
        f"The following text is narration from a documentary.  Is this "
        f"narration BOTH on-topic for '{_TOPIC}' AND written in "
        f"English ({_LANGUAGE})?\n\nNarration:\n---\n{narration}\n---\n\n"
        "Answer with a single word: yes or no."
    )
    runs = judge_text_deterministic_yes(prompt)
    disabled = [r for r in runs if r.disabled]
    assert not disabled, (
        f"Gemini judge was disabled on {len(disabled)} of 3 runs: "
        f"{[r.error for r in disabled]}"
    )
    verdicts = {r.is_yes for r in runs}
    assert verdicts == {True}, (
        f"Gemini judge was not deterministic on an on-topic scenario: "
        f"answers={[r.answer[:60] for r in runs]}"
    )


@requires_google_api
def test_gemini_judge_rejects_clearly_off_topic_narration() -> None:
    """The contra-case: a scenario that is *clearly* off-topic must be rejected.

    No LLM generation here — we construct a deliberately off-topic
    narration (recipe for chocolate cake while the topic is hyperinflation).
    This proves the judge does not rubber-stamp "yes" for any input.
    """
    off_topic_narration = (
        "Preheat your oven to 350 degrees Fahrenheit.  In a large bowl, "
        "combine two cups of flour, one and a half cups of sugar, and "
        "a pinch of salt.  Whisk the dry ingredients together, then "
        "add eggs, milk, and melted butter.  Pour the batter into a "
        "greased pan and bake for thirty minutes.  Let the cake cool "
        "on a wire rack before frosting with chocolate ganache."
    )
    prompt = (
        f"The following text is narration from a documentary.  Is this "
        f"narration BOTH on-topic for '{_TOPIC}' AND written in "
        f"English ({_LANGUAGE})?\n\nNarration:\n---\n{off_topic_narration}"
        f"\n---\n\nAnswer with a single word: yes or no."
    )
    judgment = judge_text_yes(prompt)
    assert not judgment.disabled, f"judge disabled: {judgment.error}"
    assert judgment.is_yes is False, (
        f"Gemini judge failed to reject clearly off-topic narration; "
        f"answer={judgment.answer!r}"
    )
