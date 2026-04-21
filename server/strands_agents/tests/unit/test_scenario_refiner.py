"""Unit tests for the scenario-refiner Strands agent (Component 03).

Covers:

* Deterministic tool behaviour (``adjust_scene_durations``,
  ``validate_pronunciation_hints``, ``persist_refined_scenes``).
* LLM-backed tool wiring (``tweak_voice_text`` + helper registry).
* :class:`SkipIfTimingPassed` hook (observability flag +
  tool cancellation).
* Hook stack wiring in :func:`build_scenario_refiner_agent`.
* Experiment-factory shape and threshold table.

Every test in this module is deterministic and offline — no LLM, no
GPU, no network. LLM-backed tools receive injected fakes.
"""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import MagicMock

import pytest

from strands.hooks import (
    AfterInvocationEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookRegistry,
)

from strands_agents.evals.experiments.scenario_refiner import (
    REFINER_OUTPUT_RUBRIC,
    REFINER_TRAJECTORY_DESCRIPTION,
    REFINER_TRAJECTORY_RUBRIC,
    SCENARIO_REFINER_EVALUATOR_THRESHOLDS,
    build_refiner_experiment,
    refiner_cases,
    refiner_evaluators,
)
from strands_agents.hooks import (
    ContractEnforcer,
    RevisionTagger,
    SkipIfTimingPassed,
)
from strands_agents.scenario_refiner import (
    SYSTEM_PROMPT,
    ScenarioRefinerHelperNotConfigured,
    adjust_scene_durations,
    build_scenario_refiner_agent,
    clear_refiner_helpers,
    persist_refined_scenes,
    set_refiner_helpers,
    tweak_voice_text,
    validate_pronunciation_hints,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scene(
    num: int,
    *,
    target: float = 60.0,
    voices: list[tuple[str, str]] | None = None,
    hints: dict[str, str] | None = None,
    include_hook: bool = False,
    include_outro: bool = False,
) -> dict[str, Any]:
    scene: dict[str, Any] = {
        "id": num,
        "scene_num": num,
        "title": f"Scene {num}",
        "target_duration_sec": target,
        "voices": [
            {"voice_id": vid, "text": text}
            for vid, text in (voices or [("V1", "Baseline narration for the scene.")])
        ],
        "pronunciation_hints": dict(hints or {"CPI": "C. P. I."}),
    }
    if include_hook:
        scene["hook_spec"] = {
            "topic_specific_motif": "ticker tape",
            "motion_description": "zoom across headlines",
            "narrative_pull": "why prices matter",
        }
    if include_outro:
        scene["outro_spec"] = {
            "closing_shot": "city skyline at dusk",
            "recap_sentence": "Prices shape behaviour.",
            "cta": "Subscribe for more.",
            "brand_card": "Economy Documentary",
        }
    return scene


def _baseline_scenes(n: int = 5) -> list[dict[str, Any]]:
    return [
        _scene(i + 1, include_hook=(i == 0), include_outro=(i == n - 1))
        for i in range(n)
    ]


class _FakeState:
    """Minimal ``agent.state`` stand-in with ``get()`` and ``set()``."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    def get(self, *keys: str) -> Any:
        if not keys:
            return dict(self._data)
        return self._data.get(keys[0])

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value


def _mk_agent(state: dict[str, Any]) -> MagicMock:
    agent = MagicMock()
    agent.state = _FakeState(state)
    return agent


@pytest.fixture(autouse=True)
def _reset_helpers() -> None:
    clear_refiner_helpers()
    yield
    clear_refiner_helpers()


# ---------------------------------------------------------------------------
# adjust_scene_durations
# ---------------------------------------------------------------------------


def test_adjust_scene_durations_replaces_targets_in_place() -> None:
    scenes = _baseline_scenes(5)
    out = adjust_scene_durations.__wrapped__(
        scenes=scenes,
        per_scene_targets={1: 55.0, 3: 48.0, 5: 70.0},
    )
    by_id = {s["id"]: s for s in out["scenes"]}
    assert by_id[1]["target_duration_sec"] == 55.0
    assert by_id[2]["target_duration_sec"] == 60.0
    assert by_id[3]["target_duration_sec"] == 48.0
    assert by_id[5]["target_duration_sec"] == 70.0
    assert sorted(out["updated_scene_ids"]) == [1, 3, 5]


def test_adjust_scene_durations_preserves_scene_cardinality_and_fields() -> None:
    scenes = _baseline_scenes(5)
    original_ids = [s["id"] for s in scenes]
    out = adjust_scene_durations.__wrapped__(
        scenes=scenes,
        per_scene_targets={i: 42.0 for i in original_ids},
    )
    assert [s["id"] for s in out["scenes"]] == original_ids
    # hook_spec on scene 1, outro_spec on scene 5 preserved
    assert "hook_spec" in out["scenes"][0]
    assert "outro_spec" in out["scenes"][-1]
    # pronunciation_hints preserved
    for scene in out["scenes"]:
        assert "pronunciation_hints" in scene


def test_adjust_scene_durations_skips_unknown_ids() -> None:
    scenes = _baseline_scenes(3)
    out = adjust_scene_durations.__wrapped__(
        scenes=scenes,
        per_scene_targets={99: 45.0, 2: 50.0},
    )
    assert out["updated_scene_ids"] == [2]
    assert out["scenes"][1]["target_duration_sec"] == 50.0


def test_adjust_scene_durations_accepts_string_keys() -> None:
    scenes = _baseline_scenes(3)
    out = adjust_scene_durations.__wrapped__(
        scenes=scenes,
        per_scene_targets={"2": 50.0},
    )
    assert out["updated_scene_ids"] == [2]


def test_adjust_scene_durations_rejects_empty_scenes() -> None:
    with pytest.raises(ValueError, match="empty"):
        adjust_scene_durations.__wrapped__(scenes=[], per_scene_targets={1: 30.0})


def test_adjust_scene_durations_rejects_nonpositive_target() -> None:
    scenes = _baseline_scenes(2)
    with pytest.raises(ValueError, match="positive"):
        adjust_scene_durations.__wrapped__(
            scenes=scenes,
            per_scene_targets={1: 0.0},
        )
    with pytest.raises(ValueError, match="positive"):
        adjust_scene_durations.__wrapped__(
            scenes=scenes,
            per_scene_targets={1: -5.0},
        )


def test_adjust_scene_durations_does_not_mutate_input() -> None:
    scenes = _baseline_scenes(3)
    original_target = scenes[0]["target_duration_sec"]
    adjust_scene_durations.__wrapped__(
        scenes=scenes,
        per_scene_targets={1: 99.0},
    )
    assert scenes[0]["target_duration_sec"] == original_target


# ---------------------------------------------------------------------------
# tweak_voice_text
# ---------------------------------------------------------------------------


def _shorten_rewriter(
    text: str, direction: Literal["shorten", "lengthen"], delta_sec: float
) -> str:
    words = text.split()
    if direction == "shorten":
        keep = max(1, int(len(words) * 0.7))
        return " ".join(words[:keep])
    return text + " Additionally, the effect compounds across cohorts over time."


def test_tweak_voice_text_raises_without_helper() -> None:
    with pytest.raises(ScenarioRefinerHelperNotConfigured):
        tweak_voice_text.__wrapped__(
            scenes=_baseline_scenes(3),
            scene_id=1,
            direction="shorten",
            delta_sec=2.0,
        )


def test_tweak_voice_text_shortens_narration_and_preserves_structure() -> None:
    set_refiner_helpers(text_rewriter=_shorten_rewriter)
    scenes = _baseline_scenes(3)
    original_voice_id = scenes[0]["voices"][0]["voice_id"]
    original_hints = dict(scenes[0]["pronunciation_hints"])

    out = tweak_voice_text.__wrapped__(
        scenes=scenes,
        scene_id=1,
        direction="shorten",
        delta_sec=3.0,
    )
    target = out["scenes"][0]
    assert target["voices"][0]["voice_id"] == original_voice_id
    assert target["pronunciation_hints"] == original_hints
    # Narration got shorter
    assert len(target["voices"][0]["text"]) < len(scenes[0]["voices"][0]["text"])
    assert out["changed_voice_count"] == 1


def test_tweak_voice_text_lengthens_narration() -> None:
    set_refiner_helpers(text_rewriter=_shorten_rewriter)
    scenes = _baseline_scenes(3)
    out = tweak_voice_text.__wrapped__(
        scenes=scenes,
        scene_id=2,
        direction="lengthen",
        delta_sec=4.0,
    )
    assert len(out["scenes"][1]["voices"][0]["text"]) > len(
        scenes[1]["voices"][0]["text"]
    )


def test_tweak_voice_text_rejects_unknown_scene_id() -> None:
    set_refiner_helpers(text_rewriter=_shorten_rewriter)
    with pytest.raises(ValueError, match="not found"):
        tweak_voice_text.__wrapped__(
            scenes=_baseline_scenes(3),
            scene_id=999,
            direction="shorten",
            delta_sec=2.0,
        )


def test_tweak_voice_text_rejects_invalid_direction() -> None:
    set_refiner_helpers(text_rewriter=_shorten_rewriter)
    with pytest.raises(ValueError, match="shorten"):
        tweak_voice_text.__wrapped__(
            scenes=_baseline_scenes(3),
            scene_id=1,
            direction="sideways",  # type: ignore[arg-type]
            delta_sec=2.0,
        )


def test_tweak_voice_text_rejects_nonpositive_delta() -> None:
    set_refiner_helpers(text_rewriter=_shorten_rewriter)
    with pytest.raises(ValueError, match="positive"):
        tweak_voice_text.__wrapped__(
            scenes=_baseline_scenes(3),
            scene_id=1,
            direction="shorten",
            delta_sec=0.0,
        )


def test_tweak_voice_text_rejects_scene_with_no_voices() -> None:
    set_refiner_helpers(text_rewriter=_shorten_rewriter)
    scenes = _baseline_scenes(3)
    scenes[0]["voices"] = []
    with pytest.raises(ValueError, match="no voices"):
        tweak_voice_text.__wrapped__(
            scenes=scenes,
            scene_id=1,
            direction="shorten",
            delta_sec=2.0,
        )


def test_tweak_voice_text_splits_delta_across_voices() -> None:
    captured: list[tuple[str, str, float]] = []

    def _capture(text: str, direction: str, delta_sec: float) -> str:
        captured.append((text, direction, delta_sec))
        return text + " trailing"

    set_refiner_helpers(text_rewriter=_capture)
    scenes = _baseline_scenes(3)
    scenes[0]["voices"] = [
        {"voice_id": "V1", "text": "one"},
        {"voice_id": "V2", "text": "two"},
        {"voice_id": "V3", "text": "three"},
    ]
    tweak_voice_text.__wrapped__(
        scenes=scenes,
        scene_id=1,
        direction="lengthen",
        delta_sec=6.0,
    )
    assert [round(c[2], 3) for c in captured] == [2.0, 2.0, 2.0]


def test_tweak_voice_text_splits_delta_across_active_voices_only() -> None:
    """Empty-text voices must not inflate the per-voice denominator.

    Counting empty voices in the split would systematically under-correct
    by the empty-voice ratio (e.g. 6s / 3 voices = 2s/voice delivered to
    the 2 active voices → 4s applied instead of 6s).
    """
    captured: list[tuple[str, str, float]] = []

    def _capture(text: str, direction: str, delta_sec: float) -> str:
        captured.append((text, direction, delta_sec))
        return text + " trailing"

    set_refiner_helpers(text_rewriter=_capture)
    scenes = _baseline_scenes(3)
    scenes[0]["voices"] = [
        {"voice_id": "V1", "text": "one"},
        {"voice_id": "V2", "text": ""},  # empty — should be skipped, and not counted
        {"voice_id": "V3", "text": "three"},
    ]
    tweak_voice_text.__wrapped__(
        scenes=scenes,
        scene_id=1,
        direction="lengthen",
        delta_sec=6.0,
    )
    # delta split across 2 active voices, not 3 total voices
    assert [round(c[2], 3) for c in captured] == [3.0, 3.0]
    total_applied = sum(c[2] for c in captured)
    assert round(total_applied, 3) == 6.0


def test_tweak_voice_text_rejects_scene_where_all_voices_are_blank() -> None:
    """If no voice carries narration the refine request is ill-formed."""
    set_refiner_helpers(text_rewriter=_shorten_rewriter)
    scenes = _baseline_scenes(3)
    scenes[0]["voices"] = [
        {"voice_id": "V1", "text": ""},
        {"voice_id": "V2", "text": "   "},
    ]
    with pytest.raises(ValueError, match="no voices carry narration"):
        tweak_voice_text.__wrapped__(
            scenes=scenes,
            scene_id=1,
            direction="shorten",
            delta_sec=2.0,
        )


# ---------------------------------------------------------------------------
# validate_pronunciation_hints
# ---------------------------------------------------------------------------


def test_validate_pronunciation_hints_passes_when_present_everywhere() -> None:
    out = validate_pronunciation_hints.__wrapped__(scenes=_baseline_scenes(5))
    assert out == {"ok": True, "missing_on": []}


def test_validate_pronunciation_hints_allows_empty_hint_dicts() -> None:
    scenes = _baseline_scenes(3)
    for scene in scenes:
        scene["pronunciation_hints"] = {}
    out = validate_pronunciation_hints.__wrapped__(scenes=scenes)
    assert out == {"ok": True, "missing_on": []}


def test_validate_pronunciation_hints_flags_missing_keys() -> None:
    scenes = _baseline_scenes(4)
    scenes[0].pop("pronunciation_hints")
    scenes[2].pop("pronunciation_hints")
    out = validate_pronunciation_hints.__wrapped__(scenes=scenes)
    assert out["ok"] is False
    assert out["missing_on"] == [1, 3]


# ---------------------------------------------------------------------------
# persist_refined_scenes
# ---------------------------------------------------------------------------


def test_persist_refined_scenes_writes_all_state_keys() -> None:
    scenes = _baseline_scenes(3)
    state: dict[str, Any] = {"timing_passed": True}
    agent = _mk_agent(state)
    tool_context = MagicMock()
    tool_context.agent = agent

    out = persist_refined_scenes.__wrapped__(scenes=scenes, tool_context=tool_context)
    assert out["persisted"] is True
    assert out["scene_count"] == 3

    # The state is now mutated on the FakeState object
    fake_state = agent.state
    assert fake_state.get("scenes") == scenes
    assert fake_state.get("timing_passed") is False
    assert fake_state.get("_audio_needs_regeneration") is True
    # scenes_json should be valid JSON
    import json

    assert json.loads(fake_state.get("scenes_json")) == scenes


def test_persist_refined_scenes_returns_same_scenes() -> None:
    scenes = _baseline_scenes(2)
    agent = _mk_agent({})
    tool_context = MagicMock()
    tool_context.agent = agent
    out = persist_refined_scenes.__wrapped__(scenes=scenes, tool_context=tool_context)
    assert out["scenes"] == scenes


# ---------------------------------------------------------------------------
# SkipIfTimingPassed hook
# ---------------------------------------------------------------------------


def test_skip_if_timing_passed_registers_both_callbacks() -> None:
    hook = SkipIfTimingPassed()
    reg = HookRegistry()
    hook.register_hooks(reg)
    before_inv = list(
        reg.get_callbacks_for(
            BeforeInvocationEvent(
                agent=_mk_agent({}), invocation_state={}, messages=None
            )
        )
    )
    assert before_inv, "BeforeInvocationEvent callback not registered"


def test_skip_if_timing_passed_sets_observability_flag_when_timing_passed() -> None:
    hook = SkipIfTimingPassed()
    invocation_state: dict[str, Any] = {}
    event = BeforeInvocationEvent(
        agent=_mk_agent({"timing_passed": True}),
        invocation_state=invocation_state,
        messages=None,
    )
    hook._on_before_invocation(event)
    assert invocation_state.get("skip_refiner") is True


def test_skip_if_timing_passed_no_flag_when_timing_not_passed() -> None:
    hook = SkipIfTimingPassed()
    invocation_state: dict[str, Any] = {}
    event = BeforeInvocationEvent(
        agent=_mk_agent({"timing_passed": False}),
        invocation_state=invocation_state,
        messages=None,
    )
    hook._on_before_invocation(event)
    assert "skip_refiner" not in invocation_state


def test_skip_if_timing_passed_handles_string_truthy() -> None:
    hook = SkipIfTimingPassed()
    invocation_state: dict[str, Any] = {}
    event = BeforeInvocationEvent(
        agent=_mk_agent({"timing_passed": "True"}),
        invocation_state=invocation_state,
        messages=None,
    )
    hook._on_before_invocation(event)
    assert invocation_state.get("skip_refiner") is True


def test_skip_if_timing_passed_cancels_tool_call() -> None:
    hook = SkipIfTimingPassed()
    selected = MagicMock()
    selected.tool_name = "adjust_scene_durations"
    event = BeforeToolCallEvent(
        agent=_mk_agent({"timing_passed": True}),
        selected_tool=selected,
        tool_use={"toolUseId": "t-1", "name": "adjust_scene_durations", "input": {}},
        invocation_state={},
    )
    hook._on_before_tool(event)
    assert event.cancel_tool
    assert "timing" in str(event.cancel_tool)


def test_skip_if_timing_passed_does_not_cancel_when_timing_not_passed() -> None:
    hook = SkipIfTimingPassed()
    selected = MagicMock()
    selected.tool_name = "adjust_scene_durations"
    event = BeforeToolCallEvent(
        agent=_mk_agent({"timing_passed": False}),
        selected_tool=selected,
        tool_use={"toolUseId": "t-2", "name": "adjust_scene_durations", "input": {}},
        invocation_state={},
    )
    hook._on_before_tool(event)
    # cancel_tool starts as False/None; ensure it wasn't set to a truthy
    # string.
    assert not event.cancel_tool


def test_skip_if_timing_passed_reads_nested_state_key() -> None:
    hook = SkipIfTimingPassed(state_key="scenario")
    invocation_state: dict[str, Any] = {}
    event = BeforeInvocationEvent(
        agent=_mk_agent({"scenario": {"timing_passed": True}}),
        invocation_state=invocation_state,
        messages=None,
    )
    hook._on_before_invocation(event)
    assert invocation_state.get("skip_refiner") is True


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------


def test_build_scenario_refiner_agent_wires_all_tools_and_hooks() -> None:
    agent = build_scenario_refiner_agent(model="openai/gpt-4o-mini")
    tool_names = set(agent.tool_names)
    assert tool_names == {
        "adjust_scene_durations",
        "tweak_voice_text",
        "validate_pronunciation_hints",
        "persist_refined_scenes",
    }
    assert agent.system_prompt == SYSTEM_PROMPT
    assert agent.name == "scenario_refiner"


def test_build_scenario_refiner_agent_conversation_manager_window() -> None:
    agent = build_scenario_refiner_agent(window_size=7)
    # The SlidingWindowConversationManager exposes window_size directly.
    assert getattr(agent.conversation_manager, "window_size", None) == 7


# ---------------------------------------------------------------------------
# Experiment factory
# ---------------------------------------------------------------------------


def test_refiner_cases_covers_all_five_slots() -> None:
    names = {c.name for c in refiner_cases()}
    assert names == {
        "timing_passed_noop",
        "shorten_single_scene",
        "lengthen_single_scene",
        "total_off_per_scene_ok",
        "preserve_pronunciation_hints",
    }


def test_refiner_cases_carry_five_scenes_each() -> None:
    for case in refiner_cases():
        assert len(case.input["scenes"]) == 5, f"case {case.name} has wrong scene count"


def test_refiner_cases_carry_pronunciation_hints() -> None:
    for case in refiner_cases():
        for scene in case.input["scenes"]:
            assert "pronunciation_hints" in scene, (
                f"case {case.name}, scene {scene.get('id')} missing pronunciation_hints"
            )


def test_refiner_cases_timing_passed_noop_expects_empty_trajectory() -> None:
    by_name = {c.name: c for c in refiner_cases()}
    case = by_name["timing_passed_noop"]
    assert case.expected_trajectory == []
    assert case.input["timing_passed"] is True
    assert case.metadata["expect_noop"] is True


def test_refiner_cases_shorten_expects_tweak_then_persist() -> None:
    case = next(c for c in refiner_cases() if c.name == "shorten_single_scene")
    assert "tweak_voice_text" in case.expected_trajectory
    assert case.expected_trajectory[-1] == "persist_refined_scenes"
    assert case.metadata["expect_shorter_scene_id"] == 3


def test_refiner_cases_proportional_expects_adjust_then_persist() -> None:
    case = next(c for c in refiner_cases() if c.name == "total_off_per_scene_ok")
    assert "adjust_scene_durations" in case.expected_trajectory
    assert case.expected_trajectory[-1] == "persist_refined_scenes"
    assert case.metadata["expect_all_scenes_updated"] is True


def test_refiner_evaluator_stack_order() -> None:
    evaluators = refiner_evaluators()
    names = [type(e).__name__ for e in evaluators]
    assert names == [
        "ContractComplianceEvaluator",
        "Contains",
        "OutputEvaluator",
        "TrajectoryEvaluator",
    ]


def test_refiner_contains_evaluator_looks_for_hints_key() -> None:
    contains = next(e for e in refiner_evaluators() if type(e).__name__ == "Contains")
    assert contains.value == "pronunciation_hints"


def test_refiner_thresholds_cover_every_evaluator() -> None:
    names = {type(e).__name__ for e in refiner_evaluators()}
    assert names == set(SCENARIO_REFINER_EVALUATOR_THRESHOLDS.keys())


def test_refiner_thresholds_hard_gates_are_contract_and_contains() -> None:
    hard = {
        k
        for k, (_, hard_gate) in SCENARIO_REFINER_EVALUATOR_THRESHOLDS.items()
        if hard_gate
    }
    assert hard == {"ContractComplianceEvaluator", "Contains"}


def test_build_refiner_experiment_matches_factory_components() -> None:
    exp = build_refiner_experiment()
    assert [c.name for c in exp.cases] == [c.name for c in refiner_cases()]
    assert [type(e).__name__ for e in exp.evaluators] == [
        type(e).__name__ for e in refiner_evaluators()
    ]


def test_refiner_rubrics_are_non_empty_strings() -> None:
    assert REFINER_OUTPUT_RUBRIC and isinstance(REFINER_OUTPUT_RUBRIC, str)
    assert REFINER_TRAJECTORY_RUBRIC and isinstance(REFINER_TRAJECTORY_RUBRIC, str)
    assert set(REFINER_TRAJECTORY_DESCRIPTION.keys()) == {
        "adjust_scene_durations",
        "tweak_voice_text",
        "validate_pronunciation_hints",
        "persist_refined_scenes",
    }


# ---------------------------------------------------------------------------
# Cross-hook integration: ContractEnforcer + RevisionTagger coexistence
# ---------------------------------------------------------------------------


def test_refiner_hook_stack_independent_instances() -> None:
    """Smoke: the three hooks can be registered together without errors."""
    reg = HookRegistry()
    ContractEnforcer.__init__  # noqa: B018 — reference to detect import errors
    for hook in [
        ContractEnforcer(
            __import__("contracts", fromlist=["SCENARIO_CONTRACT"]).SCENARIO_CONTRACT,
            check_preconditions=False,
        ),
        SkipIfTimingPassed(),
        RevisionTagger("scenes", stage="scenario_refiner", retag_on_reproduce=True),
    ]:
        hook.register_hooks(reg)

    # Each event type fires at least one callback.
    before = list(
        reg.get_callbacks_for(
            BeforeInvocationEvent(
                agent=_mk_agent({}), invocation_state={}, messages=None
            )
        )
    )
    after = list(
        reg.get_callbacks_for(
            AfterInvocationEvent(
                agent=_mk_agent({}), invocation_state={}, result=None, resume=None
            )
        )
    )
    assert before, "no BeforeInvocationEvent callbacks registered"
    assert after, "no AfterInvocationEvent callbacks registered"
