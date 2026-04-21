"""Unit tests for the content-analyst Strands agent (Component 06).

Covers:

* Deterministic tool behaviour (``validate_phrases``,
  ``persist_content_analysis``, ``_phrase_id`` stability,
  ``_clamp_time_span``).
* LLM-backed tool wiring (``extract_phrases`` + helper registry).
* Hook stack wiring in :func:`build_content_analyst_agent`.
* Experiment-factory shape and threshold table.

Every test is deterministic and offline — no LLM, no GPU, no network.
The LLM-backed ``extract_phrases`` receives an injected fake extractor.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from strands.hooks import (
    AfterInvocationEvent,
    BeforeInvocationEvent,
    HookRegistry,
)

from contracts import VISUAL_DIRECTION_CONTRACT
from strands_agents.content_analyst import (
    MIN_PHRASES_PER_SCENE,
    NARRATIVE_WEIGHTS,
    PHRASE_TYPES,
    ContentAnalystHelperNotConfigured,
    _clamp_time_span,
    _phrase_id,
    _scene_num,
    _segment_bounds,
    build_content_analyst_agent,
    clear_content_analyst_helpers,
    extract_phrases,
    persist_content_analysis,
    set_content_analyst_helpers,
    validate_phrases,
)
from strands_agents.evals.experiments.content_analyst import (
    CONTENT_ANALYST_EVALUATOR_THRESHOLDS,
    build_content_analyst_experiment,
    content_analyst_cases,
    content_analyst_evaluators,
)
from strands_agents.hooks import ContractEnforcer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _segment(scene_num: int, *, start: float, end: float) -> dict[str, Any]:
    return {"scene_num": scene_num, "start": start, "end": end}


def _scene(scene_num: int, *, text: str) -> dict[str, Any]:
    return {
        "scene_num": scene_num,
        "id": scene_num,
        "voices": [{"voice_id": "V1", "text": text}],
    }


def _fake_extractor_factory(
    phrases_per_scene: list[dict[str, Any]],
):
    """Return a deterministic extractor that yields a fixed phrase list."""

    def _extractor(
        scene: dict[str, Any],
        segment: dict[str, Any],
        max_phrases: int,
    ) -> list[dict[str, Any]]:
        return phrases_per_scene[:max_phrases]

    return _extractor


@pytest.fixture(autouse=True)
def _reset_helpers():
    clear_content_analyst_helpers()
    yield
    clear_content_analyst_helpers()


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def test_phrase_id_is_stable_across_calls() -> None:
    a = _phrase_id(2, 3, "central banks raise rates")
    b = _phrase_id(2, 3, "central banks raise rates")
    assert a == b
    assert a.startswith("ph-02-03-")
    assert len(a.split("-")[-1]) == 10


def test_phrase_id_varies_with_scene_phrase_and_text() -> None:
    base = _phrase_id(1, 0, "same text")
    assert _phrase_id(2, 0, "same text") != base
    assert _phrase_id(1, 1, "same text") != base
    assert _phrase_id(1, 0, "different text") != base


def test_scene_num_prefers_id_key() -> None:
    # Key order matches audio_tool._scene_num and
    # scenario_refiner._find_scene_index so scene identity is stable
    # across the three components.
    assert _scene_num({"scene_num": 3, "id": 99}) == 99


def test_scene_num_falls_back_to_scene_num_when_id_is_none() -> None:
    # Regression: `if key in scene` without a None-guard previously called
    # int(None) and raised TypeError instead of falling through to the
    # next candidate key.
    assert _scene_num({"id": None, "scene_num": 5}) == 5


def test_scene_num_falls_back_to_scene_id_when_others_missing() -> None:
    assert _scene_num({"scene_id": 7}) == 7


def test_scene_num_raises_value_error_when_all_keys_missing() -> None:
    with pytest.raises(ValueError, match="missing scene_num"):
        _scene_num({"voices": []})


def test_scene_num_raises_value_error_on_non_int_convertible() -> None:
    with pytest.raises(ValueError, match="int-convertible"):
        _scene_num({"scene_num": "not-a-number"})


def test_clamp_time_span_within_bounds() -> None:
    assert _clamp_time_span([1.0, 3.0], 0.0, 5.0) == [1.0, 3.0]


def test_clamp_time_span_clips_to_segment() -> None:
    assert _clamp_time_span([-1.0, 10.0], 0.0, 5.0) == [0.0, 5.0]


def test_clamp_time_span_bad_input_falls_back_to_segment() -> None:
    assert _clamp_time_span("nope", 0.0, 4.0) == [0.0, 4.0]
    assert _clamp_time_span([1.0], 0.0, 4.0) == [0.0, 4.0]


def test_clamp_time_span_handles_inverted_order() -> None:
    # If the extractor produces end < start inside the segment we clamp
    # end to start (zero-duration phrase) rather than propagating chaos.
    result = _clamp_time_span([3.0, 1.0], 0.0, 5.0)
    assert result == [3.0, 3.0]


def test_segment_bounds_raises_on_missing_keys() -> None:
    with pytest.raises(ValueError, match="missing start/end"):
        _segment_bounds({"scene_num": 1})


def test_segment_bounds_raises_on_inverted_range() -> None:
    with pytest.raises(ValueError, match="malformed"):
        _segment_bounds({"start": 5.0, "end": 1.0})


# ---------------------------------------------------------------------------
# extract_phrases tool
# ---------------------------------------------------------------------------


def test_extract_phrases_raises_without_helper() -> None:
    with pytest.raises(ContentAnalystHelperNotConfigured):
        extract_phrases.__wrapped__(
            _scene(1, text="something"),
            _segment(1, start=0.0, end=5.0),
        )


def test_extract_phrases_stamps_phrase_ids_and_clamps_time_spans() -> None:
    set_content_analyst_helpers(
        phrase_extractor=_fake_extractor_factory(
            [
                {
                    "text": "phrase one",
                    "phrase_type": "concept",
                    "narrative_weight": "hook",
                    "visual_intent": "open on skyline",
                    "word_span": [0, 2],
                    "time_span": [-1.0, 2.0],
                },
                {
                    "text": "phrase two",
                    "phrase_type": "process",
                    "narrative_weight": "build",
                    "visual_intent": "close on ledger",
                    "word_span": [2, 4],
                    "time_span": [2.0, 10.0],
                },
            ]
        )
    )
    result = extract_phrases.__wrapped__(
        _scene(1, text="phrase one phrase two"),
        _segment(1, start=0.0, end=6.0),
    )
    assert result["scene_num"] == 1
    assert [p["phrase_id"] for p in result["phrases"]] == [
        _phrase_id(1, 0, "phrase one"),
        _phrase_id(1, 1, "phrase two"),
    ]
    # First phrase's negative start clamps to 0; second's 10s end clamps to 6s.
    assert result["phrases"][0]["time_span"] == [0.0, 2.0]
    assert result["phrases"][1]["time_span"] == [2.0, 6.0]


def test_extract_phrases_truncates_to_max_phrases() -> None:
    set_content_analyst_helpers(
        phrase_extractor=_fake_extractor_factory(
            [
                {
                    "text": f"p{i}",
                    "phrase_type": "concept",
                    "narrative_weight": "build",
                    "visual_intent": "",
                    "word_span": [i, i + 1],
                    "time_span": [float(i), float(i + 1)],
                }
                for i in range(8)
            ]
        )
    )
    result = extract_phrases.__wrapped__(
        _scene(1, text="irrelevant"),
        _segment(1, start=0.0, end=10.0),
        max_phrases=3,
    )
    assert len(result["phrases"]) == 3


def test_extract_phrases_rejects_max_phrases_below_one() -> None:
    set_content_analyst_helpers(
        phrase_extractor=_fake_extractor_factory([])
    )
    with pytest.raises(ValueError, match="max_phrases"):
        extract_phrases.__wrapped__(
            _scene(1, text="anything"),
            _segment(1, start=0.0, end=5.0),
            max_phrases=0,
        )


# ---------------------------------------------------------------------------
# validate_phrases tool
# ---------------------------------------------------------------------------


def _good_phrase(
    *,
    weight: str = "build",
    ptype: str = "concept",
    start: float = 0.0,
    end: float = 1.0,
) -> dict[str, Any]:
    return {
        "phrase_id": "ph-01-00-deadbeef01",
        "text": "some phrase",
        "phrase_type": ptype,
        "narrative_weight": weight,
        "visual_intent": "a shot",
        "word_span": [0, 2],
        "time_span": [start, end],
    }


def test_validate_phrases_happy_path() -> None:
    analysis = {
        "per_scene": [
            {
                "scene_num": 1,
                "phrases": [_good_phrase(weight="hook", start=0.0, end=2.0)],
            },
            {
                "scene_num": 2,
                "phrases": [_good_phrase(weight="payoff", start=2.0, end=4.0)],
            },
        ]
    }
    result = validate_phrases.__wrapped__(analysis)
    assert result == {"valid": True, "issues": []}


def test_validate_phrases_empty_per_scene_fails() -> None:
    result = validate_phrases.__wrapped__({"per_scene": []})
    assert result["valid"] is False
    assert result["issues"][0]["code"] == "empty_per_scene"


def test_validate_phrases_missing_phrases_raises_no_phrases_issue() -> None:
    analysis = {
        "per_scene": [
            {"scene_num": 1, "phrases": []},
            {
                "scene_num": 2,
                "phrases": [_good_phrase(weight="payoff")],
            },
        ]
    }
    result = validate_phrases.__wrapped__(analysis)
    assert result["valid"] is False
    codes = {issue["code"] for issue in result["issues"]}
    assert "no_phrases" in codes


def test_validate_phrases_flags_bad_enum_values() -> None:
    analysis = {
        "per_scene": [
            {
                "scene_num": 1,
                "phrases": [
                    _good_phrase(weight="hook", ptype="nonsense"),
                ],
            },
            {
                "scene_num": 2,
                "phrases": [
                    _good_phrase(weight="nope", start=2.0, end=3.0),
                ],
            },
        ]
    }
    result = validate_phrases.__wrapped__(analysis)
    assert result["valid"] is False
    codes = {issue["code"] for issue in result["issues"]}
    assert "bad_phrase_type" in codes
    assert "bad_narrative_weight" in codes


def test_validate_phrases_detects_overlap_and_inversion() -> None:
    analysis = {
        "per_scene": [
            {
                "scene_num": 1,
                "phrases": [
                    _good_phrase(weight="hook", start=0.0, end=2.0),
                    # second phrase starts before the first ended
                    _good_phrase(weight="build", start=1.5, end=3.0),
                    # third phrase has end < start
                    _good_phrase(weight="payoff", start=5.0, end=4.0),
                ],
            },
            {
                "scene_num": 2,
                "phrases": [_good_phrase(weight="payoff", start=5.0, end=6.0)],
            },
        ]
    }
    result = validate_phrases.__wrapped__(analysis)
    codes = {issue["code"] for issue in result["issues"]}
    assert "overlapping_time_span" in codes
    assert "inverted_time_span" in codes


def test_validate_phrases_requires_hook_and_payoff_at_edges() -> None:
    analysis = {
        "per_scene": [
            {
                "scene_num": 1,
                "phrases": [_good_phrase(weight="build")],
            },
            {
                "scene_num": 2,
                "phrases": [_good_phrase(weight="build", start=1.0, end=2.0)],
            },
        ]
    }
    result = validate_phrases.__wrapped__(analysis)
    codes = {issue["code"] for issue in result["issues"]}
    assert "missing_hook" in codes
    assert "missing_payoff" in codes


def test_validate_phrases_flags_malformed_time_span() -> None:
    analysis = {
        "per_scene": [
            {
                "scene_num": 1,
                "phrases": [
                    {
                        **_good_phrase(weight="hook"),
                        "time_span": "not-a-span",
                    }
                ],
            },
            {
                "scene_num": 2,
                "phrases": [_good_phrase(weight="payoff", start=2.0, end=3.0)],
            },
        ]
    }
    result = validate_phrases.__wrapped__(analysis)
    codes = {issue["code"] for issue in result["issues"]}
    assert "bad_time_span" in codes


# ---------------------------------------------------------------------------
# persist_content_analysis tool
# ---------------------------------------------------------------------------


def test_persist_writes_structured_and_json_onto_state() -> None:
    state: dict[str, Any] = {}

    tool_context = MagicMock()
    tool_context.agent.state.set.side_effect = lambda k, v: state.__setitem__(k, v)

    analysis = {
        "per_scene": [
            {
                "scene_num": 1,
                "phrases": [_good_phrase(weight="hook")],
            },
            {
                "scene_num": 2,
                "phrases": [
                    _good_phrase(weight="build"),
                    _good_phrase(weight="payoff", start=2.0, end=3.0),
                ],
            },
        ]
    }

    result = persist_content_analysis.__wrapped__(analysis, tool_context)

    assert result == {
        "persisted": True,
        "scene_count": 2,
        "phrase_count": 3,
    }
    assert state["content_analysis"] == analysis
    # Roundtrips through JSON so downstream ADK-style readers still work.
    assert json.loads(state["content_analysis_json"]) == analysis


def test_persist_snapshots_instead_of_storing_reference() -> None:
    state: dict[str, Any] = {}
    tool_context = MagicMock()
    tool_context.agent.state.set.side_effect = lambda k, v: state.__setitem__(k, v)

    analysis = {
        "per_scene": [
            {"scene_num": 1, "phrases": [_good_phrase(weight="hook")]}
        ]
    }
    persist_content_analysis.__wrapped__(analysis, tool_context)

    # Mutating the input after the call must not affect state.
    analysis["per_scene"].append({"scene_num": 99, "phrases": []})
    stored = state["content_analysis"]
    assert [entry["scene_num"] for entry in stored["per_scene"]] == [1]


# ---------------------------------------------------------------------------
# Agent builder + hook wiring
# ---------------------------------------------------------------------------


def test_build_agent_default_exposes_expected_tools_and_prompt() -> None:
    agent = build_content_analyst_agent()
    assert agent.name == "content_analyst"
    assert set(agent.tool_names) == {
        "extract_phrases",
        "validate_phrases",
        "persist_content_analysis",
    }
    assert agent.system_prompt.startswith("You are the Content Analyst")


def test_build_agent_conversation_manager_window_defaults_to_thirty() -> None:
    agent = build_content_analyst_agent()
    assert getattr(agent.conversation_manager, "window_size", None) == 30


def test_build_agent_custom_window_size_passed_through() -> None:
    agent = build_content_analyst_agent(window_size=12)
    assert getattr(agent.conversation_manager, "window_size", None) == 12


def _callback_count(registry: HookRegistry, event: Any) -> int:
    return sum(1 for _ in registry.get_callbacks_for(event))


def _baseline_counts() -> tuple[int, int]:
    """Callback counts for a bare Agent with no user-provided hooks.

    The Strands SDK registers internal model-loop / retry callbacks on
    ``AfterInvocationEvent``; any user-hook assertion compares the
    delta against this baseline instead of asserting absolute zeros.
    """
    from strands import Agent as _Agent

    bare = _Agent(name="baseline", tools=[])
    before = BeforeInvocationEvent(agent=MagicMock())
    after = AfterInvocationEvent(agent=MagicMock())
    return (
        _callback_count(bare.hooks, before),
        _callback_count(bare.hooks, after),
    )


def test_build_agent_default_registers_contract_pre_not_post() -> None:
    base_before, base_after = _baseline_counts()
    agent = build_content_analyst_agent()
    before = BeforeInvocationEvent(agent=MagicMock())
    after = AfterInvocationEvent(agent=MagicMock())
    # ContractEnforcer registers exactly one preinvocation callback.
    assert _callback_count(agent.hooks, before) == base_before + 1
    # check_postconditions=False: no additional after-invocation callbacks.
    assert _callback_count(agent.hooks, after) == base_after


def test_build_agent_with_tag_revisions_subscribes_after_invocation() -> None:
    base_before, base_after = _baseline_counts()
    agent = build_content_analyst_agent(tag_revisions=True)
    before = BeforeInvocationEvent(agent=MagicMock())
    after = AfterInvocationEvent(agent=MagicMock())
    # Contract preinvocation + RevisionTagger preinvocation seeding.
    assert _callback_count(agent.hooks, before) >= base_before + 1
    # RevisionTagger adds exactly one after-invocation callback.
    assert _callback_count(agent.hooks, after) == base_after + 1


def test_build_agent_without_contract_or_tags_registers_no_user_hooks() -> None:
    base_before, base_after = _baseline_counts()
    agent = build_content_analyst_agent(
        enforce_contract=False, tag_revisions=False
    )
    before = BeforeInvocationEvent(agent=MagicMock())
    after = AfterInvocationEvent(agent=MagicMock())
    assert _callback_count(agent.hooks, before) == base_before
    assert _callback_count(agent.hooks, after) == base_after


def test_contract_enforcer_rejects_preinvocation_without_required_state() -> None:
    """Direct ContractEnforcer wiring: preconditions gate missing state."""
    enforcer = ContractEnforcer(
        VISUAL_DIRECTION_CONTRACT, check_postconditions=False
    )
    registry = HookRegistry()
    enforcer.register_hooks(registry)

    event_agent = MagicMock()
    event_agent.state.get.return_value = {}  # no scenes, no whisperx_alignment
    event = BeforeInvocationEvent(agent=event_agent)
    with pytest.raises(Exception):
        registry.invoke_callbacks(event)


def test_contract_enforcer_with_postconditions_off_ignores_after_event() -> None:
    """After-invocation is a no-op when check_postconditions=False."""
    enforcer = ContractEnforcer(
        VISUAL_DIRECTION_CONTRACT, check_postconditions=False
    )
    registry = HookRegistry()
    enforcer.register_hooks(registry)

    event_agent = MagicMock()
    event_agent.state.get.return_value = {}  # would fail postcond if checked
    event = AfterInvocationEvent(agent=event_agent)
    registry.invoke_callbacks(event)  # must not raise


# ---------------------------------------------------------------------------
# Experiment factory
# ---------------------------------------------------------------------------


def test_experiment_exposes_five_cases_with_expected_names() -> None:
    cases = content_analyst_cases()
    names = [c.name for c in cases]
    assert names == [
        "standard_5_scenes",
        "data_heavy_scene",
        "short_scene_10s",
        "multi_voice_scene",
        "missing_alignment",
    ]


def test_each_case_input_carries_scenes_and_alignment() -> None:
    for case in content_analyst_cases():
        assert "scenes" in case.input
        assert "whisperx_alignment" in case.input


def test_experiment_evaluator_stack_and_thresholds_align() -> None:
    evaluators = content_analyst_evaluators()
    eval_names = {type(e).__name__ for e in evaluators}
    # Every evaluator in the stack has a threshold entry.
    assert eval_names == set(CONTENT_ANALYST_EVALUATOR_THRESHOLDS.keys())

    # Hard gates: contract + trajectory. Soft: output, faithfulness.
    assert CONTENT_ANALYST_EVALUATOR_THRESHOLDS["ContractComplianceEvaluator"] == (1.0, True)
    assert CONTENT_ANALYST_EVALUATOR_THRESHOLDS["TrajectoryEvaluator"][1] is True
    assert CONTENT_ANALYST_EVALUATOR_THRESHOLDS["OutputEvaluator"][1] is False
    assert CONTENT_ANALYST_EVALUATOR_THRESHOLDS["FaithfulnessEvaluator"][1] is False


def test_build_experiment_returns_populated_experiment() -> None:
    exp = build_content_analyst_experiment()
    assert len(exp.cases) == 5
    assert {type(e).__name__ for e in exp.evaluators} == set(
        CONTENT_ANALYST_EVALUATOR_THRESHOLDS.keys()
    )


def test_vocabulary_constants_match_spec() -> None:
    # Guardrails for accidental drift between the spec and the code.
    assert PHRASE_TYPES == {
        "concept",
        "entity",
        "process",
        "transition",
        "data",
    }
    assert NARRATIVE_WEIGHTS == {
        "hook",
        "build",
        "payoff",
        "connective",
    }
    assert MIN_PHRASES_PER_SCENE == 1
