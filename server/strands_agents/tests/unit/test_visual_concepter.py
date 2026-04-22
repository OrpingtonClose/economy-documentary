"""Unit tests for the visual-concepter Strands agent (Component 07).

Covers:

* Deterministic tool behaviour (``check_style_lock``,
  ``persist_visual_concepts``, duration clamp, negative-prompt merge).
* LLM-backed tool wiring (``propose_concept`` + helper registry).
* :class:`StyleLockEnforcer` hook behaviour on
  :class:`AfterToolCallEvent`.
* Hook stack wiring in :func:`build_visual_concepter_agent`.
* Experiment-factory shape and threshold table.

Every test is deterministic and offline — no LLM, no GPU, no network.
The LLM-backed ``propose_concept`` receives an injected fake proposer.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from strands.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    HookRegistry,
)

from contracts import VISUAL_DIRECTION_CONTRACT
from strands_agents.evals.experiments.visual_concepter import (
    VISUAL_CONCEPTER_EVALUATOR_THRESHOLDS,
    build_visual_concepter_experiment,
    visual_concepter_cases,
    visual_concepter_evaluators,
)
from strands_agents.hooks import ContractEnforcer
from strands_agents.visual_concepter import (
    CAMERA_MOVEMENTS,
    MAX_CLIP_DURATION_SEC,
    MIN_CLIP_DURATION_SEC,
    SHOT_TYPES,
    StyleLockEnforcer,
    VisualConcepterHelperNotConfigured,
    _clamp_duration,
    _compose_negative_prompt,
    _phrase_duration,
    build_visual_concepter_agent,
    check_style_lock,
    clear_visual_concepter_helpers,
    persist_visual_concepts,
    propose_concept,
    set_visual_concepter_helpers,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _cinematic_lock() -> dict[str, Any]:
    return {
        "dominant_style": "cinematic_documentary",
        "positive_fragment": "shot on 35mm with shallow depth of field",
        "forbidden_styles": ["anime", "cartoon"],
        "palette": ["warm tungsten"],
    }


def _visual_style() -> dict[str, Any]:
    return {
        "style": "cinematic documentary",
        "avoid": ["stock cliches", "cheesy transitions"],
    }


def _phrase(
    *,
    scene_id: int = 1,
    phrase_idx: int = 0,
    phrase_type: str = "concept",
    text: str = "a quiet observation",
    time_span: tuple[float, float] = (0.0, 4.0),
) -> dict[str, Any]:
    return {
        "phrase_id": f"ph-{scene_id:02d}-{phrase_idx:02d}-deadbeef00",
        "scene_id": scene_id,
        "scene_num": scene_id,
        "phrase_type": phrase_type,
        "narrative_weight": "build",
        "visual_intent": text,
        "text": text,
        "word_span": [0, 5],
        "time_span": list(time_span),
    }


def _good_concept(
    *,
    phrase_id: str = "ph-01-00-deadbeef00",
    scene_id: int = 1,
    shot_type: str = "medium",
    camera_movement: str = "locked",
    prompt_extra: str = "A measured wide shot of a trading floor",
    style_lock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lock = style_lock or _cinematic_lock()
    return {
        "phrase_id": phrase_id,
        "scene_id": scene_id,
        "shot_type": shot_type,
        "camera_movement": camera_movement,
        "prompt": f"{prompt_extra}, {lock['positive_fragment']}.",
        "negative_prompt": "anime, cartoon, text",
        "duration_sec": 4.0,
        "style_lock_applied": True,
        "ltx_params": {"resolution": [1280, 720], "seed": None, "steps": 30},
    }


def _fake_proposer_factory(concept_override: dict[str, Any] | None = None):
    """Return a deterministic proposer that always returns a known concept."""

    def _proposer(
        phrase: dict[str, Any],
        style_lock: dict[str, Any],
        visual_style: dict[str, Any],
    ) -> dict[str, Any]:
        if concept_override is not None:
            return dict(concept_override)
        return {
            "shot_type": "medium",
            "camera_movement": "locked",
            "prompt": (
                f"A measured medium shot, {style_lock.get('positive_fragment', '')}."
            ),
            "negative_prompt": "blurry, low resolution",
            "duration_sec": phrase.get("time_span", [0, 4])[1]
            - phrase.get("time_span", [0, 4])[0],
            "ltx_params": {"resolution": [1280, 720], "seed": None, "steps": 30},
        }

    return _proposer


@pytest.fixture(autouse=True)
def _reset_helpers():
    clear_visual_concepter_helpers()
    yield
    clear_visual_concepter_helpers()


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def test_clamp_duration_within_bounds() -> None:
    assert _clamp_duration(4.5) == 4.5


def test_clamp_duration_clips_low() -> None:
    assert _clamp_duration(0.2) == MIN_CLIP_DURATION_SEC


def test_clamp_duration_clips_high() -> None:
    assert _clamp_duration(42.0) == MAX_CLIP_DURATION_SEC


def test_clamp_duration_handles_bad_inputs() -> None:
    assert _clamp_duration(None) == MIN_CLIP_DURATION_SEC
    assert _clamp_duration("bad") == MIN_CLIP_DURATION_SEC


def test_phrase_duration_uses_time_span_delta() -> None:
    assert _phrase_duration({"time_span": [1.0, 5.5]}) == 4.5


def test_phrase_duration_clamps_to_max() -> None:
    assert _phrase_duration({"time_span": [0.0, 99.0]}) == MAX_CLIP_DURATION_SEC


def test_phrase_duration_falls_back_on_missing_span() -> None:
    assert _phrase_duration({}) == MIN_CLIP_DURATION_SEC


def test_compose_negative_prompt_merges_forbidden_and_avoid() -> None:
    merged = _compose_negative_prompt(
        "blurry, low res",
        _cinematic_lock(),
        _visual_style(),
    )
    lower = merged.lower()
    assert "blurry" in lower
    assert "anime" in lower
    assert "cartoon" in lower
    assert "stock cliches" in lower
    assert "watermark" in lower  # standard LTX deny-list


def test_compose_negative_prompt_deduplicates_tokens() -> None:
    merged = _compose_negative_prompt(
        "anime, anime, TEXT",
        {"forbidden_styles": ["anime"]},
        {"avoid": ["anime"]},
    )
    tokens = [t.strip() for t in merged.split(",")]
    # Case-insensitive dedup: "anime" (or "ANIME") appears once.
    assert sum(1 for t in tokens if t.lower() == "anime") == 1


def test_compose_negative_prompt_accepts_list_inputs() -> None:
    merged = _compose_negative_prompt(
        ["foo", "bar"],
        {"forbidden_styles": ["baz"]},
        {"avoid": ["qux"]},
    )
    lower = merged.lower()
    for token in ("foo", "bar", "baz", "qux", "watermark"):
        assert token in lower


# ---------------------------------------------------------------------------
# propose_concept (LLM-backed via injected helper)
# ---------------------------------------------------------------------------


def test_propose_concept_requires_helper() -> None:
    with pytest.raises(VisualConcepterHelperNotConfigured):
        propose_concept.__wrapped__(
            _phrase(), _cinematic_lock(), _visual_style()
        )


def test_propose_concept_stamps_phrase_and_scene_ids() -> None:
    set_visual_concepter_helpers(concept_proposer=_fake_proposer_factory())
    concept = propose_concept.__wrapped__(
        _phrase(scene_id=3, phrase_idx=2),
        _cinematic_lock(),
        _visual_style(),
    )
    assert concept["phrase_id"] == "ph-03-02-deadbeef00"
    assert concept["scene_id"] == 3
    assert concept["style_lock_applied"] is True
    assert concept["ltx_params"]["resolution"] == [1280, 720]


def test_propose_concept_falls_back_to_phrase_time_span_for_duration() -> None:
    def _no_duration_proposer(phrase, style_lock, visual_style):
        return {
            "shot_type": "medium",
            "camera_movement": "locked",
            "prompt": f"A steady shot, {style_lock['positive_fragment']}.",
        }

    set_visual_concepter_helpers(concept_proposer=_no_duration_proposer)
    concept = propose_concept.__wrapped__(
        _phrase(time_span=(2.0, 6.5)),
        _cinematic_lock(),
        _visual_style(),
    )
    assert concept["duration_sec"] == 4.5


def test_propose_concept_clamps_helper_duration() -> None:
    def _oversize_proposer(phrase, style_lock, visual_style):
        return {
            "shot_type": "wide",
            "camera_movement": "dolly_in",
            "prompt": f"Wide establishing, {style_lock['positive_fragment']}.",
            "duration_sec": 99.0,
        }

    set_visual_concepter_helpers(concept_proposer=_oversize_proposer)
    concept = propose_concept.__wrapped__(
        _phrase(), _cinematic_lock(), _visual_style()
    )
    assert concept["duration_sec"] == MAX_CLIP_DURATION_SEC


def test_propose_concept_merges_negative_prompt_with_forbidden_styles() -> None:
    set_visual_concepter_helpers(concept_proposer=_fake_proposer_factory())
    concept = propose_concept.__wrapped__(
        _phrase(), _cinematic_lock(), _visual_style()
    )
    assert "anime" in concept["negative_prompt"].lower()
    assert "cartoon" in concept["negative_prompt"].lower()
    assert "watermark" in concept["negative_prompt"].lower()


def test_propose_concept_rejects_phrase_missing_identifiers() -> None:
    set_visual_concepter_helpers(concept_proposer=_fake_proposer_factory())
    with pytest.raises(ValueError):
        propose_concept.__wrapped__(
            {"scene_id": 1, "time_span": [0, 1]},
            _cinematic_lock(),
            _visual_style(),
        )


# ---------------------------------------------------------------------------
# check_style_lock (deterministic)
# ---------------------------------------------------------------------------


def test_check_style_lock_happy_path() -> None:
    concepts = [
        _good_concept(scene_id=1, camera_movement="locked"),
        _good_concept(
            phrase_id="ph-01-01-cafebabe",
            scene_id=1,
            shot_type="wide",
            camera_movement="dolly_in",
        ),
    ]
    result = check_style_lock.__wrapped__(concepts, _cinematic_lock())
    assert result == {"ok": True, "violations": []}


def test_check_style_lock_flags_empty_concepts() -> None:
    result = check_style_lock.__wrapped__([], _cinematic_lock())
    assert result["ok"] is False
    assert any(v["code"] == "empty_concepts" for v in result["violations"])


def test_check_style_lock_flags_missing_positive_fragment() -> None:
    bad = _good_concept()
    bad["prompt"] = "A shot with no style lock keywords."
    result = check_style_lock.__wrapped__([bad], _cinematic_lock())
    assert result["ok"] is False
    assert any(
        v["code"] == "missing_positive_fragment" for v in result["violations"]
    )


def test_check_style_lock_flags_forbidden_style_in_prompt() -> None:
    bad = _good_concept()
    bad["prompt"] = (
        "An anime-style wide shot, shot on 35mm with shallow depth of field."
    )
    result = check_style_lock.__wrapped__([bad], _cinematic_lock())
    assert result["ok"] is False
    assert any(
        v["code"] == "forbidden_style_in_prompt" for v in result["violations"]
    )


def test_check_style_lock_flags_bad_shot_type() -> None:
    bad = _good_concept(shot_type="invented_shot")
    result = check_style_lock.__wrapped__([bad], _cinematic_lock())
    assert result["ok"] is False
    assert any(v["code"] == "bad_shot_type" for v in result["violations"])


def test_check_style_lock_flags_bad_camera_movement() -> None:
    bad = _good_concept(camera_movement="zoom_warp")
    result = check_style_lock.__wrapped__([bad], _cinematic_lock())
    assert result["ok"] is False
    assert any(
        v["code"] == "bad_camera_movement" for v in result["violations"]
    )


def test_check_style_lock_flags_style_lock_applied_false() -> None:
    bad = _good_concept()
    bad["style_lock_applied"] = False
    result = check_style_lock.__wrapped__([bad], _cinematic_lock())
    assert result["ok"] is False
    assert any(
        v["code"] == "style_lock_not_applied" for v in result["violations"]
    )


def test_check_style_lock_flags_repeated_camera_movement_in_scene() -> None:
    first = _good_concept(scene_id=1, camera_movement="dolly_in")
    second = _good_concept(
        phrase_id="ph-01-01-cafebabe",
        scene_id=1,
        camera_movement="dolly_in",
    )
    result = check_style_lock.__wrapped__([first, second], _cinematic_lock())
    assert result["ok"] is False
    assert any(
        v["code"] == "repeated_camera_movement" for v in result["violations"]
    )


def test_check_style_lock_allows_repeated_locked_shots() -> None:
    first = _good_concept(scene_id=1, camera_movement="locked")
    second = _good_concept(
        phrase_id="ph-01-01-cafebabe",
        scene_id=1,
        camera_movement="locked",
    )
    result = check_style_lock.__wrapped__([first, second], _cinematic_lock())
    assert result == {"ok": True, "violations": []}


# ---------------------------------------------------------------------------
# persist_visual_concepts
# ---------------------------------------------------------------------------


def test_persist_writes_concepts_and_json_to_state() -> None:
    state: dict[str, Any] = {}
    tool_context = MagicMock()
    tool_context.agent.state.set.side_effect = lambda k, v: state.__setitem__(k, v)

    concepts = [_good_concept()]
    result = persist_visual_concepts.__wrapped__(concepts, tool_context)

    assert result == {"persisted": True, "concept_count": 1}
    assert state["visual_concepts"] == concepts
    assert json.loads(state["visual_concepts_json"]) == concepts


def test_persist_snapshots_instead_of_storing_reference() -> None:
    state: dict[str, Any] = {}
    tool_context = MagicMock()
    tool_context.agent.state.set.side_effect = lambda k, v: state.__setitem__(k, v)

    concepts = [_good_concept()]
    persist_visual_concepts.__wrapped__(concepts, tool_context)

    concepts.append(_good_concept(phrase_id="injected-after"))
    stored = state["visual_concepts"]
    assert [c["phrase_id"] for c in stored] == ["ph-01-00-deadbeef00"]


# ---------------------------------------------------------------------------
# StyleLockEnforcer
# ---------------------------------------------------------------------------


def _tool_event(
    *,
    tool_name: str,
    concept: dict[str, Any] | None,
    style_lock: dict[str, Any] | None,
) -> AfterToolCallEvent:
    agent = MagicMock()
    agent.state.get.side_effect = lambda key, *a, **k: (
        style_lock if key == "style_lock" else None
    )
    result: Any
    if concept is None:
        result = {"status": "success", "content": []}
    else:
        result = {"status": "success", "content": [{"json": concept}]}
    return AfterToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": tool_name, "input": {}},
        invocation_state={},
        result=result,
    )


def test_style_lock_enforcer_passes_clean_concept() -> None:
    enforcer = StyleLockEnforcer()
    event = _tool_event(
        tool_name="propose_concept",
        concept=_good_concept(),
        style_lock=_cinematic_lock(),
    )
    enforcer._on_after_tool(event)
    assert event.retry is False


def test_style_lock_enforcer_retries_forbidden_style() -> None:
    enforcer = StyleLockEnforcer()
    bad = _good_concept()
    bad["prompt"] = "An anime-style wide shot with shot on 35mm shallow depth."
    event = _tool_event(
        tool_name="propose_concept",
        concept=bad,
        style_lock=_cinematic_lock(),
    )
    enforcer._on_after_tool(event)
    assert event.retry is True


def test_style_lock_enforcer_retries_missing_positive_fragment() -> None:
    enforcer = StyleLockEnforcer()
    bad = _good_concept()
    bad["prompt"] = "A wide shot with no style keywords."
    event = _tool_event(
        tool_name="propose_concept",
        concept=bad,
        style_lock=_cinematic_lock(),
    )
    enforcer._on_after_tool(event)
    assert event.retry is True


def test_style_lock_enforcer_ignores_other_tools() -> None:
    enforcer = StyleLockEnforcer()
    bad = _good_concept()
    bad["prompt"] = "Broken anime reference."
    event = _tool_event(
        tool_name="persist_visual_concepts",
        concept=bad,
        style_lock=_cinematic_lock(),
    )
    enforcer._on_after_tool(event)
    assert event.retry is False


def test_style_lock_enforcer_skips_when_no_style_lock_on_state() -> None:
    enforcer = StyleLockEnforcer()
    bad = _good_concept()
    bad["prompt"] = "Anime style ignoring the lock."
    event = _tool_event(
        tool_name="propose_concept",
        concept=bad,
        style_lock=None,
    )
    enforcer._on_after_tool(event)
    assert event.retry is False


def test_style_lock_enforcer_honours_retry_budget() -> None:
    enforcer = StyleLockEnforcer(max_retries=2)
    lock = _cinematic_lock()
    attempts = 0
    for _ in range(5):
        bad = _good_concept()
        bad["prompt"] = "Cartoon styled overview of the newsroom."
        event = _tool_event(
            tool_name="propose_concept",
            concept=bad,
            style_lock=lock,
        )
        enforcer._on_after_tool(event)
        if event.retry:
            attempts += 1
    assert attempts == 2


def test_style_lock_enforcer_resets_counter_after_clean_concept() -> None:
    enforcer = StyleLockEnforcer(max_retries=1)
    lock = _cinematic_lock()

    bad = _good_concept()
    bad["prompt"] = "Cartoon overview of the newsroom."
    event_bad = _tool_event(
        tool_name="propose_concept", concept=bad, style_lock=lock
    )
    enforcer._on_after_tool(event_bad)
    assert event_bad.retry is True

    event_ok = _tool_event(
        tool_name="propose_concept",
        concept=_good_concept(),
        style_lock=lock,
    )
    enforcer._on_after_tool(event_ok)
    assert event_ok.retry is False

    # Budget is reset for this phrase_id; next drift triggers retry again.
    bad2 = _good_concept()
    bad2["prompt"] = "Cartoon overview of the newsroom again."
    event_bad2 = _tool_event(
        tool_name="propose_concept", concept=bad2, style_lock=lock
    )
    enforcer._on_after_tool(event_bad2)
    assert event_bad2.retry is True


# ---------------------------------------------------------------------------
# Agent builder + hook wiring (baseline-delta pattern)
# ---------------------------------------------------------------------------


def test_build_agent_default_exposes_expected_tools_and_prompt() -> None:
    agent = build_visual_concepter_agent()
    assert agent.name == "visual_concepter"
    assert set(agent.tool_names) == {
        "propose_concept",
        "check_style_lock",
        "persist_visual_concepts",
    }
    assert agent.system_prompt.startswith("You are the Visual Concepter")


def test_build_agent_default_window_size_is_forty() -> None:
    agent = build_visual_concepter_agent()
    assert getattr(agent.conversation_manager, "window_size", None) == 40


def test_build_agent_custom_window_size_passed_through() -> None:
    agent = build_visual_concepter_agent(window_size=16)
    assert getattr(agent.conversation_manager, "window_size", None) == 16


def _callback_count(registry: HookRegistry, event: Any) -> int:
    return sum(1 for _ in registry.get_callbacks_for(event))


def _baseline_counts() -> tuple[int, int, int]:
    """Callback counts for a bare Agent with no user-provided hooks."""
    from strands import Agent as _Agent

    bare = _Agent(name="baseline", tools=[])
    before = BeforeInvocationEvent(agent=MagicMock())
    after = AfterInvocationEvent(agent=MagicMock())
    after_tool = AfterToolCallEvent(
        agent=MagicMock(),
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": "noop", "input": {}},
        invocation_state={},
        result={"status": "success", "content": []},
    )
    return (
        _callback_count(bare.hooks, before),
        _callback_count(bare.hooks, after),
        _callback_count(bare.hooks, after_tool),
    )


def test_build_agent_default_registers_contract_and_style_lock() -> None:
    base_before, base_after, base_after_tool = _baseline_counts()
    agent = build_visual_concepter_agent()
    before = BeforeInvocationEvent(agent=MagicMock())
    after = AfterInvocationEvent(agent=MagicMock())
    after_tool = AfterToolCallEvent(
        agent=MagicMock(),
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": "noop", "input": {}},
        invocation_state={},
        result={"status": "success", "content": []},
    )
    # ContractEnforcer registers pre + post callbacks (check_postconditions=True).
    assert _callback_count(agent.hooks, before) == base_before + 1
    assert _callback_count(agent.hooks, after) == base_after + 1
    # StyleLockEnforcer registers exactly one AfterToolCallEvent callback.
    assert _callback_count(agent.hooks, after_tool) == base_after_tool + 1


def test_build_agent_with_tag_revisions_adds_another_after_invocation() -> None:
    base_before, base_after, _ = _baseline_counts()
    agent = build_visual_concepter_agent(tag_revisions=True)
    before = BeforeInvocationEvent(agent=MagicMock())
    after = AfterInvocationEvent(agent=MagicMock())
    # Contract preinvocation + RevisionTagger preinvocation seeding.
    assert _callback_count(agent.hooks, before) >= base_before + 1
    # Contract postinvocation + RevisionTagger postinvocation = +2.
    assert _callback_count(agent.hooks, after) == base_after + 2


def test_build_agent_without_hooks_registers_no_user_callbacks() -> None:
    base_before, base_after, base_after_tool = _baseline_counts()
    agent = build_visual_concepter_agent(
        enforce_contract=False,
        enforce_style_lock=False,
        tag_revisions=False,
    )
    before = BeforeInvocationEvent(agent=MagicMock())
    after = AfterInvocationEvent(agent=MagicMock())
    after_tool = AfterToolCallEvent(
        agent=MagicMock(),
        selected_tool=None,
        tool_use={"toolUseId": "t-1", "name": "noop", "input": {}},
        invocation_state={},
        result={"status": "success", "content": []},
    )
    assert _callback_count(agent.hooks, before) == base_before
    assert _callback_count(agent.hooks, after) == base_after
    assert _callback_count(agent.hooks, after_tool) == base_after_tool


def test_contract_enforcer_rejects_missing_scenes() -> None:
    """Direct ContractEnforcer wiring: preconditions gate missing state."""
    enforcer = ContractEnforcer(VISUAL_DIRECTION_CONTRACT)
    registry = HookRegistry()
    enforcer.register_hooks(registry)

    event_agent = MagicMock()
    event_agent.state.get.return_value = {}  # no scenes, no whisperx_alignment
    event = BeforeInvocationEvent(agent=event_agent)
    with pytest.raises(Exception):
        for cb in registry.get_callbacks_for(event):
            cb(event)


# ---------------------------------------------------------------------------
# Experiment factory
# ---------------------------------------------------------------------------


def test_experiment_cases_cover_all_five_scenarios() -> None:
    cases = visual_concepter_cases()
    names = {c.name for c in cases}
    assert names == {
        "cinematic_doc",
        "hand_drawn",
        "realism_anchor",
        "forbidden_style",
        "phrase_data_heavy",
    }


def test_experiment_evaluator_stack_has_four_evaluators() -> None:
    evaluators = visual_concepter_evaluators()
    names = [type(e).__name__ for e in evaluators]
    assert names == [
        "ContractComplianceEvaluator",
        "VisualCoherenceEvaluator",
        "ToolSelectionAccuracyEvaluator",
        "CoherenceEvaluator",
    ]


def test_experiment_round_trip_assembles_cases_and_evaluators() -> None:
    experiment = build_visual_concepter_experiment()
    assert len(experiment.cases) == 5
    assert len(experiment.evaluators) == 4


def test_threshold_table_matches_evaluator_stack() -> None:
    evaluator_names = {
        type(e).__name__ for e in visual_concepter_evaluators()
    }
    assert set(VISUAL_CONCEPTER_EVALUATOR_THRESHOLDS.keys()) == evaluator_names
    # Hard gate is the contract evaluator only.
    hard_gates = {
        name
        for name, (_, is_hard) in VISUAL_CONCEPTER_EVALUATOR_THRESHOLDS.items()
        if is_hard
    }
    assert hard_gates == {"ContractComplianceEvaluator"}


def test_forbidden_style_case_flagged_with_retry_metadata() -> None:
    cases = {c.name: c for c in visual_concepter_cases()}
    forbidden = cases["forbidden_style"]
    assert forbidden.metadata["expect_style_lock_retry"] is True
    # The case still expects check_style_lock + persist_visual_concepts at the
    # end of a clean trajectory — retries are observed via OTel, not via
    # additional entries in expected_trajectory.
    assert forbidden.expected_trajectory[-2:] == [
        "check_style_lock",
        "persist_visual_concepts",
    ]


def test_shot_type_and_camera_movement_vocabularies_are_non_empty() -> None:
    # Guard against accidentally shrinking the vocabulary — the system
    # prompt hard-codes these enumerations.
    assert len(SHOT_TYPES) >= 10
    assert len(CAMERA_MOVEMENTS) >= 10
