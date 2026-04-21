"""Unit tests for the coherence-evaluator Strands agent (Component 08).

Covers:

* Deterministic structural checks (``_structural_violations``,
  ``_forbidden_tokens``, ``_expected_phrase_ids``, ``_normalise_rating``).
* LLM-backed tool wiring (``score_visual_coherence`` + helper registry).
* Persistence (``persist_coherence_report`` writes state keys + re-derives
  ``visual_coherence_passed`` from rating).
* Hook stack wiring in :func:`build_coherence_evaluator_agent`.
* Experiment-factory shape and threshold table.

Every test is deterministic and offline — no LLM, no GPU, no network.
``score_visual_coherence`` receives an injected fake soft scorer.
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
from strands_agents.coherence_evaluator import (
    MAX_CONSECUTIVE_IDENTICAL_SHOTS,
    CoherenceEvaluatorHelperNotConfigured,
    _expected_phrase_ids,
    _forbidden_tokens,
    _normalise_rating,
    _structural_violations,
    build_coherence_evaluator_agent,
    clear_coherence_evaluator_helpers,
    persist_coherence_report,
    score_visual_coherence,
    set_coherence_evaluator_helpers,
)
from strands_agents.evals.experiments.coherence_evaluator import (
    COHERENCE_EVALUATOR_THRESHOLDS,
    build_coherence_evaluator_experiment,
    coherence_evaluator_cases,
    coherence_evaluator_evaluators,
)
from strands_agents.hooks import ContractEnforcer


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _style_lock(**overrides: Any) -> dict[str, Any]:
    base = {
        "dominant_style": "cinematic_documentary",
        "positive_fragment": "shot on 35mm film with shallow depth of field",
        "negative_fragment": "cartoon, anime, illustration",
        "forbidden_styles": ["anime", "cartoon", "cyberpunk"],
        "palette": ["warm tungsten", "soft daylight"],
        "realism_anchors": ["4K", "no CGI"],
    }
    base.update(overrides)
    return base


def _phrase(scene: int, idx: int, text: str = "phrase") -> dict[str, Any]:
    return {
        "phrase_id": f"ph-{scene:02d}-{idx:02d}-text",
        "scene_id": scene,
        "scene_num": scene,
        "phrase_type": "concept",
        "narrative_weight": "build",
        "visual_intent": text,
        "text": text,
        "word_span": [0, 1],
        "time_span": [0.0, 3.0],
    }


def _content_analysis(phrases: list[dict[str, Any]]) -> dict[str, Any]:
    per_scene: dict[int, list[dict[str, Any]]] = {}
    for phrase in phrases:
        per_scene.setdefault(phrase["scene_num"], []).append(phrase)
    return {
        "per_scene": [
            {"scene_num": s, "phrases": ps}
            for s, ps in sorted(per_scene.items())
        ]
    }


def _concept(
    phrase: dict[str, Any],
    *,
    shot_type: str = "medium",
    camera_movement: str = "dolly_in",
    prompt: str | None = None,
) -> dict[str, Any]:
    return {
        "phrase_id": phrase["phrase_id"],
        "scene_id": phrase["scene_id"],
        "shot_type": shot_type,
        "camera_movement": camera_movement,
        "prompt": prompt
        or (
            "shot on 35mm film with shallow depth of field. "
            f"{phrase['visual_intent']}"
        ),
        "negative_prompt": "cartoon, anime, illustration",
        "duration_sec": 3.0,
        "ltx_params": {"resolution": [1280, 720], "seed": None, "steps": 30},
        "style_lock_applied": True,
    }


def _fake_scorer(
    *,
    rating: str = "GOOD",
    issues: list[str] | None = None,
    suggestions: list[str] | None = None,
):
    def _scorer(
        visual_concepts: list[dict[str, Any]],
        style_lock: dict[str, Any],
        content_analysis: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "rating": rating,
            "issues": list(issues or []),
            "suggestions": list(suggestions or []),
        }

    return _scorer


@pytest.fixture(autouse=True)
def _reset_helpers() -> Any:
    clear_coherence_evaluator_helpers()
    yield
    clear_coherence_evaluator_helpers()


# ---------------------------------------------------------------------------
# _forbidden_tokens
# ---------------------------------------------------------------------------


def test_forbidden_tokens_lowercases_and_trims() -> None:
    lock = _style_lock(forbidden_styles=[" Anime ", "CARTOON", "", None])
    assert _forbidden_tokens(lock) == ["anime", "cartoon"]


def test_forbidden_tokens_returns_empty_when_missing() -> None:
    assert _forbidden_tokens({"forbidden_styles": None}) == []
    assert _forbidden_tokens({}) == []


# ---------------------------------------------------------------------------
# _expected_phrase_ids
# ---------------------------------------------------------------------------


def test_expected_phrase_ids_preserves_scene_then_phrase_order() -> None:
    analysis = _content_analysis(
        [
            _phrase(1, 0, "a"),
            _phrase(2, 0, "b"),
            _phrase(2, 1, "c"),
        ]
    )
    assert _expected_phrase_ids(analysis) == [
        "ph-01-00-text",
        "ph-02-00-text",
        "ph-02-01-text",
    ]


def test_expected_phrase_ids_skips_missing_or_non_string_ids() -> None:
    analysis = {
        "per_scene": [
            {
                "scene_num": 1,
                "phrases": [
                    {"phrase_id": "keep"},
                    {"phrase_id": None},
                    {"phrase_id": 42},
                    {},
                ],
            }
        ]
    }
    assert _expected_phrase_ids(analysis) == ["keep"]


# ---------------------------------------------------------------------------
# _normalise_rating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("excellent", "EXCELLENT"),
        (" Good ", "GOOD"),
        ("FAIR", "FAIR"),
        ("poor", "POOR"),
        ("unknown", "UNKNOWN"),
        ("", "UNKNOWN"),
        (None, "UNKNOWN"),
        (7, "UNKNOWN"),
        ("great", "UNKNOWN"),
    ],
)
def test_normalise_rating(raw: Any, expected: str) -> None:
    assert _normalise_rating(raw) == expected


# ---------------------------------------------------------------------------
# _structural_violations — hard invariants
# ---------------------------------------------------------------------------


def test_structural_clean_case_returns_empty() -> None:
    phrases = [_phrase(s, 0, f"scene-{s}") for s in range(1, 6)]
    analysis = _content_analysis(phrases)
    shots = ["wide", "medium", "insert", "close_up", "wide"]
    moves = ["locked", "dolly_in", "graphic_overlay", "handheld", "orbit"]
    concepts = [
        _concept(p, shot_type=s, camera_movement=m)
        for p, s, m in zip(phrases, shots, moves, strict=True)
    ]
    assert _structural_violations(concepts, _style_lock(), analysis) == []


def test_structural_missing_concept_flagged() -> None:
    phrases = [_phrase(s, 0) for s in range(1, 4)]
    analysis = _content_analysis(phrases)
    concepts = [_concept(phrases[0]), _concept(phrases[1])]
    violations = _structural_violations(concepts, _style_lock(), analysis)
    assert any(
        "missing a visual concept" in v and phrases[2]["phrase_id"] in v
        for v in violations
    )


def test_structural_duplicate_concept_flagged() -> None:
    phrases = [_phrase(1, 0), _phrase(2, 0)]
    analysis = _content_analysis(phrases)
    concepts = [
        _concept(phrases[0]),
        _concept(phrases[0]),  # duplicate
        _concept(phrases[1]),
    ]
    violations = _structural_violations(concepts, _style_lock(), analysis)
    assert any("covered by 2 concepts" in v for v in violations)


def test_structural_unknown_phrase_flagged() -> None:
    phrases = [_phrase(1, 0)]
    analysis = _content_analysis(phrases)
    concepts = [
        _concept(phrases[0]),
        _concept({**_phrase(9, 9), "phrase_id": "ph-ghost"}),
    ]
    violations = _structural_violations(concepts, _style_lock(), analysis)
    assert any("not present in content_analysis" in v for v in violations)


def test_structural_forbidden_style_token_flagged() -> None:
    phrase = _phrase(1, 0)
    analysis = _content_analysis([phrase])
    concept = _concept(phrase, prompt="a bright anime cel wash across frame")
    violations = _structural_violations(
        [concept], _style_lock(), analysis
    )
    assert any("forbidden style 'anime'" in v for v in violations)


def test_structural_forbidden_match_is_case_insensitive() -> None:
    phrase = _phrase(1, 0)
    analysis = _content_analysis([phrase])
    concept = _concept(phrase, prompt="Neon CYBERPUNK cityscape at night")
    violations = _structural_violations(
        [concept], _style_lock(), analysis
    )
    assert any("forbidden style 'cyberpunk'" in v for v in violations)


def test_structural_allows_three_consecutive_identical_shots() -> None:
    phrases = [_phrase(s, 0) for s in range(1, 5)]
    analysis = _content_analysis(phrases)
    shots = ["medium", "medium", "medium", "wide"]
    moves = ["dolly_in", "dolly_in", "dolly_in", "orbit"]
    concepts = [
        _concept(p, shot_type=s, camera_movement=m)
        for p, s, m in zip(phrases, shots, moves, strict=True)
    ]
    assert _structural_violations(concepts, _style_lock(), analysis) == []


def test_structural_flags_four_consecutive_identical_shots() -> None:
    phrases = [_phrase(s, 0) for s in range(1, 6)]
    analysis = _content_analysis(phrases)
    shots = ["medium"] * 4 + ["wide"]
    moves = ["dolly_in"] * 4 + ["orbit"]
    concepts = [
        _concept(p, shot_type=s, camera_movement=m)
        for p, s, m in zip(phrases, shots, moves, strict=True)
    ]
    violations = _structural_violations(concepts, _style_lock(), analysis)
    assert MAX_CONSECUTIVE_IDENTICAL_SHOTS == 3
    assert any(">3 in a row" in v for v in violations)


def test_structural_run_resets_after_break() -> None:
    # 3 identical + 1 break + 3 identical → both runs within limit.
    phrases = [_phrase(s, 0) for s in range(1, 8)]
    analysis = _content_analysis(phrases)
    shots = [
        "medium",
        "medium",
        "medium",
        "wide",
        "medium",
        "medium",
        "medium",
    ]
    moves = [
        "dolly_in",
        "dolly_in",
        "dolly_in",
        "orbit",
        "dolly_in",
        "dolly_in",
        "dolly_in",
    ]
    concepts = [
        _concept(p, shot_type=s, camera_movement=m)
        for p, s, m in zip(phrases, shots, moves, strict=True)
    ]
    violations = _structural_violations(concepts, _style_lock(), analysis)
    assert violations == []


# ---------------------------------------------------------------------------
# score_visual_coherence tool
# ---------------------------------------------------------------------------


def test_score_raises_without_helper() -> None:
    phrase = _phrase(1, 0)
    analysis = _content_analysis([phrase])
    with pytest.raises(CoherenceEvaluatorHelperNotConfigured):
        score_visual_coherence.__wrapped__(
            [_concept(phrase)], _style_lock(), analysis
        )


def test_score_empty_concepts_short_circuits_poor() -> None:
    result = score_visual_coherence.__wrapped__([], _style_lock(), {})
    assert result["rating"] == "POOR"
    assert result["visual_coherence_passed"] is False
    assert "empty" in result["issues"][0]


def test_score_clean_inputs_returns_soft_rating() -> None:
    set_coherence_evaluator_helpers(soft_scorer=_fake_scorer(rating="GOOD"))
    phrases = [_phrase(s, 0) for s in range(1, 4)]
    analysis = _content_analysis(phrases)
    shots = ["wide", "medium", "insert"]
    moves = ["locked", "dolly_in", "graphic_overlay"]
    concepts = [
        _concept(p, shot_type=s, camera_movement=m)
        for p, s, m in zip(phrases, shots, moves, strict=True)
    ]
    result = score_visual_coherence.__wrapped__(
        concepts, _style_lock(), analysis
    )
    assert result["rating"] == "GOOD"
    assert result["visual_coherence_passed"] is True
    assert result["issues"] == []


def test_score_hard_violation_forces_poor_regardless_of_soft_scorer() -> None:
    set_coherence_evaluator_helpers(
        soft_scorer=_fake_scorer(rating="EXCELLENT")
    )
    phrase = _phrase(1, 0)
    analysis = _content_analysis([phrase])
    concept = _concept(phrase, prompt="anime cel wash over the frame")
    result = score_visual_coherence.__wrapped__(
        [concept], _style_lock(), analysis
    )
    assert result["rating"] == "POOR"
    assert result["visual_coherence_passed"] is False
    assert any("forbidden style" in issue for issue in result["issues"])


def test_score_merges_soft_issues_and_suggestions_into_output() -> None:
    set_coherence_evaluator_helpers(
        soft_scorer=_fake_scorer(
            rating="FAIR",
            issues=["palette drift on scene 3"],
            suggestions=["shift scene 3 towards warm tungsten"],
        )
    )
    phrases = [_phrase(s, 0) for s in range(1, 3)]
    analysis = _content_analysis(phrases)
    concepts = [
        _concept(phrases[0], shot_type="wide", camera_movement="locked"),
        _concept(phrases[1], shot_type="medium", camera_movement="dolly_in"),
    ]
    result = score_visual_coherence.__wrapped__(
        concepts, _style_lock(), analysis
    )
    assert result["rating"] == "FAIR"
    assert result["visual_coherence_passed"] is False
    assert "palette drift on scene 3" in result["issues"]
    assert "shift scene 3 towards warm tungsten" in result["suggestions"]


def test_score_unknown_soft_rating_defaults_to_fair_when_no_hard_violations() -> None:
    set_coherence_evaluator_helpers(
        soft_scorer=_fake_scorer(rating="UNKNOWN")
    )
    phrases = [_phrase(s, 0) for s in range(1, 3)]
    analysis = _content_analysis(phrases)
    concepts = [
        _concept(phrases[0], shot_type="wide", camera_movement="locked"),
        _concept(phrases[1], shot_type="medium", camera_movement="dolly_in"),
    ]
    result = score_visual_coherence.__wrapped__(
        concepts, _style_lock(), analysis
    )
    # No hard violations → we fall back to FAIR rather than trusting
    # UNKNOWN through to the caller.
    assert result["rating"] == "FAIR"
    assert result["visual_coherence_passed"] is False


def test_score_repetitive_shot_run_triggers_hard_violation() -> None:
    set_coherence_evaluator_helpers(soft_scorer=_fake_scorer(rating="GOOD"))
    phrases = [_phrase(s, 0) for s in range(1, 6)]
    analysis = _content_analysis(phrases)
    concepts = [
        _concept(p, shot_type="medium", camera_movement="dolly_in")
        for p in phrases[:4]
    ] + [_concept(phrases[4], shot_type="wide", camera_movement="orbit")]
    result = score_visual_coherence.__wrapped__(
        concepts, _style_lock(), analysis
    )
    assert result["rating"] == "POOR"
    assert result["visual_coherence_passed"] is False
    assert any(">3 in a row" in issue for issue in result["issues"])


# ---------------------------------------------------------------------------
# persist_coherence_report tool
# ---------------------------------------------------------------------------


def _wire_state() -> tuple[MagicMock, dict[str, Any]]:
    state: dict[str, Any] = {}
    tool_context = MagicMock()
    tool_context.agent.state.set.side_effect = (
        lambda k, v: state.__setitem__(k, v)
    )
    return tool_context, state


def test_persist_writes_structured_and_json_onto_state() -> None:
    tool_context, state = _wire_state()
    report = {
        "rating": "GOOD",
        "issues": [],
        "suggestions": [],
        "visual_coherence_passed": True,
    }
    result = persist_coherence_report.__wrapped__(report, tool_context)
    assert result == {
        "persisted": True,
        "rating": "GOOD",
        "passed": True,
        "issue_count": 0,
    }
    assert state["visual_coherence_report"]["rating"] == "GOOD"
    assert state["visual_coherence_passed"] is True
    assert json.loads(state["visual_coherence_report_json"])[
        "rating"
    ] == "GOOD"


def test_persist_rederives_passed_from_rating_ignoring_caller_bool() -> None:
    tool_context, state = _wire_state()
    # Caller lies: rating=POOR but passed=True. Persist must trust rating.
    report = {
        "rating": "POOR",
        "issues": ["anything"],
        "suggestions": [],
        "visual_coherence_passed": True,
    }
    result = persist_coherence_report.__wrapped__(report, tool_context)
    assert result["passed"] is False
    assert state["visual_coherence_passed"] is False
    assert state["visual_coherence_report"]["visual_coherence_passed"] is False


def test_persist_normalises_rating_casing() -> None:
    tool_context, state = _wire_state()
    report = {
        "rating": "excellent",
        "issues": [],
        "suggestions": [],
        "visual_coherence_passed": True,
    }
    persist_coherence_report.__wrapped__(report, tool_context)
    assert state["visual_coherence_report"]["rating"] == "EXCELLENT"


def test_persist_snapshots_instead_of_storing_reference() -> None:
    tool_context, state = _wire_state()
    report: dict[str, Any] = {
        "rating": "GOOD",
        "issues": ["keep this"],
        "suggestions": [],
        "visual_coherence_passed": True,
    }
    persist_coherence_report.__wrapped__(report, tool_context)
    report["issues"].append("should not appear in state")
    assert state["visual_coherence_report"]["issues"] == ["keep this"]


def test_persist_malformed_rating_becomes_unknown_and_fails() -> None:
    tool_context, state = _wire_state()
    report = {
        "rating": "great",
        "issues": [],
        "suggestions": [],
        "visual_coherence_passed": True,
    }
    result = persist_coherence_report.__wrapped__(report, tool_context)
    assert result["rating"] == "UNKNOWN"
    assert result["passed"] is False
    assert state["visual_coherence_passed"] is False


# ---------------------------------------------------------------------------
# Agent builder + hook wiring
# ---------------------------------------------------------------------------


def test_build_agent_exposes_expected_tools_and_prompt() -> None:
    agent = build_coherence_evaluator_agent()
    assert agent.name == "coherence_evaluator"
    assert set(agent.tool_names) == {
        "score_visual_coherence",
        "persist_coherence_report",
    }
    assert agent.system_prompt.startswith(
        "You are the Coherence Evaluator"
    )


def test_build_agent_conversation_manager_window_defaults_to_ten() -> None:
    agent = build_coherence_evaluator_agent()
    assert getattr(agent.conversation_manager, "window_size", None) == 10


def test_build_agent_custom_window_size_passed_through() -> None:
    agent = build_coherence_evaluator_agent(window_size=25)
    assert getattr(agent.conversation_manager, "window_size", None) == 25


def _callback_count(registry: HookRegistry, event: Any) -> int:
    return sum(1 for _ in registry.get_callbacks_for(event))


def _baseline_counts() -> tuple[int, int]:
    """Callback counts for a bare Agent with no user-provided hooks."""
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
    agent = build_coherence_evaluator_agent()
    before = BeforeInvocationEvent(agent=MagicMock())
    after = AfterInvocationEvent(agent=MagicMock())
    assert _callback_count(agent.hooks, before) == base_before + 1
    # check_postconditions=False → no extra after-invocation callbacks.
    assert _callback_count(agent.hooks, after) == base_after


def test_build_agent_contract_enforcer_is_configured_for_preconditions() -> None:
    agent = build_coherence_evaluator_agent()
    enforcers = [
        cb
        for cb in agent.hooks.get_callbacks_for(
            BeforeInvocationEvent(agent=MagicMock())
        )
    ]
    # At least one preinvocation callback must come from a
    # ContractEnforcer bound to the VISUAL_DIRECTION contract.
    enforcer_owners = {
        getattr(cb, "__self__", None) for cb in enforcers
    }
    contract_enforcers = [
        owner
        for owner in enforcer_owners
        if isinstance(owner, ContractEnforcer)
    ]
    assert contract_enforcers, "no ContractEnforcer registered"
    assert any(
        owner.contract is VISUAL_DIRECTION_CONTRACT  # type: ignore[union-attr]
        for owner in contract_enforcers
    )


def test_build_agent_without_contract_skips_contract_hook() -> None:
    base_before, _ = _baseline_counts()
    agent = build_coherence_evaluator_agent(enforce_contract=False)
    before = BeforeInvocationEvent(agent=MagicMock())
    assert _callback_count(agent.hooks, before) == base_before


def test_build_agent_with_tag_revisions_subscribes_after_invocation() -> None:
    base_before, base_after = _baseline_counts()
    agent = build_coherence_evaluator_agent(tag_revisions=True)
    before = BeforeInvocationEvent(agent=MagicMock())
    after = AfterInvocationEvent(agent=MagicMock())
    assert _callback_count(agent.hooks, before) >= base_before + 1
    assert _callback_count(agent.hooks, after) == base_after + 1


# ---------------------------------------------------------------------------
# Experiment factory shape
# ---------------------------------------------------------------------------


def test_experiment_factory_builds_five_cases() -> None:
    cases = coherence_evaluator_cases()
    assert [c.name for c in cases] == [
        "clean_concepts",
        "style_lock_violation",
        "repetitive_shots",
        "missing_visual",
        "minor_palette_drift",
    ]


def test_experiment_factory_attaches_three_evaluators() -> None:
    evaluators = coherence_evaluator_evaluators()
    names = [type(e).__name__ for e in evaluators]
    assert names == [
        "ContractComplianceEvaluator",
        "OutputEvaluator",
        "VisualCoherenceEvaluator",
    ]


def test_build_experiment_assembles_cases_and_evaluators() -> None:
    experiment = build_coherence_evaluator_experiment()
    assert len(experiment.cases) == 5
    assert len(experiment.evaluators) == 3


def test_thresholds_table_matches_evaluator_names() -> None:
    assert set(COHERENCE_EVALUATOR_THRESHOLDS) == {
        "ContractComplianceEvaluator",
        "OutputEvaluator",
        "VisualCoherenceEvaluator",
    }
    # Contract is a hard gate at 1.0; others are soft at 0.75.
    assert COHERENCE_EVALUATOR_THRESHOLDS["ContractComplianceEvaluator"] == (
        1.0,
        True,
    )
    assert COHERENCE_EVALUATOR_THRESHOLDS["OutputEvaluator"] == (0.75, False)
    assert COHERENCE_EVALUATOR_THRESHOLDS["VisualCoherenceEvaluator"] == (
        0.75,
        False,
    )


def test_cases_have_expected_visual_direction_preconditions() -> None:
    # VISUAL_DIRECTION_CONTRACT.required_state = ["scenes", "whisperx_alignment"]
    for case in coherence_evaluator_cases():
        assert "scenes" in case.input, case.name
        assert "whisperx_alignment" in case.input, case.name
        assert "visual_concepts" in case.input, case.name


def test_clean_case_metadata_expects_passing_rating() -> None:
    (clean,) = [
        c for c in coherence_evaluator_cases() if c.name == "clean_concepts"
    ]
    assert clean.metadata["expected_passed"] is True
    assert clean.metadata["expected_hard_violations"] == 0


def test_missing_visual_case_marks_phrase_id_in_metadata() -> None:
    (case,) = [
        c for c in coherence_evaluator_cases() if c.name == "missing_visual"
    ]
    missing_id = case.metadata["missing_phrase_id"]
    covered = {c["phrase_id"] for c in case.input["visual_concepts"]}
    assert missing_id not in covered


def test_style_lock_violation_case_has_forbidden_token_in_prompt() -> None:
    (case,) = [
        c
        for c in coherence_evaluator_cases()
        if c.name == "style_lock_violation"
    ]
    token = case.metadata["forbidden_token_in_prompt"]
    prompts = [
        c["prompt"].lower() for c in case.input["visual_concepts"]
    ]
    assert any(token in p for p in prompts)


def test_repetitive_shots_case_has_long_identical_run() -> None:
    (case,) = [
        c for c in coherence_evaluator_cases() if c.name == "repetitive_shots"
    ]
    concepts = case.input["visual_concepts"]
    keys = [
        (c["shot_type"], c["camera_movement"]) for c in concepts
    ]
    run_lengths: list[int] = []
    current = 1
    for a, b in zip(keys, keys[1:], strict=False):
        if a == b:
            current += 1
        else:
            run_lengths.append(current)
            current = 1
    run_lengths.append(current)
    assert (
        max(run_lengths) > MAX_CONSECUTIVE_IDENTICAL_SHOTS
    ), "repetitive_shots case should break the consecutive-shot invariant"
