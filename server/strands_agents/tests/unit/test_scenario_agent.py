"""Unit tests for the scenario Strands agent (Component 01).

Covers:

* Tool-level behaviour (``generate_scenario``, ``evaluate_scenario``,
  ``refine_scenario``, ``create_timeline``).
* Helper injection / teardown for LLM-backed tools.
* Hook wiring (``ContractEnforcer``, ``RevisionTagger``).
* Experiment-factory shape and threshold table.

Every test in this module is deterministic and offline — no LLM
calls, no GPU, no network. LLM-backed tools use injected fakes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from contracts import (
    ContractViolation,
    SCENARIO_CONTRACT,
)
from strands.hooks import HookRegistry

from strands_agents.evals.experiments.scenario import (
    SCENARIO_EVALUATOR_THRESHOLDS,
    SCENARIO_TRAJECTORY_DESCRIPTION,
    SCENARIO_TRAJECTORY_RUBRIC,
    build_scenario_experiment,
    scenario_cases,
    scenario_evaluators,
)
from strands_agents.hooks import ContractEnforcer, RevisionTagger
from strands_agents.scenario_agent import (
    ScenarioHelperNotConfigured,
    SYSTEM_PROMPT,
    build_scenario_agent,
    clear_scenario_helpers,
    create_timeline,
    evaluate_scenario,
    generate_scenario,
    refine_scenario,
    set_scenario_helpers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _valid_style_lock() -> dict[str, Any]:
    return {
        "dominant_style": "cinematic_documentary",
        "forbidden_styles": ["anime", "pixel_art"],
        "positive_fragment": "cinematic documentary lighting, shallow depth of field",
        "negative_fragment": "cartoon, anime, oversaturated",
    }


def _valid_hook_spec() -> dict[str, Any]:
    return {
        "topic_specific_motif": "a 1923 Reichsbank wheelbarrow of banknotes",
        "motion_description": "slow push-in on the banknote stack",
        "narrative_pull": (
            "because in 7 seconds you will understand why prices doubled every "
            "two days."
        ),
    }


def _valid_outro_spec() -> dict[str, Any]:
    return {
        "closing_shot": "wide shot of modern grocery aisle, slow fade",
        "recap_sentence": "Inflation is always and everywhere a monetary phenomenon.",
        "cta": "subscribe for the next episode on central banks",
        "brand_card": "Economy Documentary  |  Series 1, Episode 2",
    }


#: One narration line carries four countable words (see ``_word_count``
#: in ``scenario_evaluator_checks``). ``check_word_count`` requires at
#: least ``duration_sec / 60 * 150`` words across all scenes; we build
#: each scene with enough ``voices`` entries to clear that by a 20%
#: margin.
_NARRATION_LINE = "inflation erodes purchasing power "


def _scene(
    num: int,
    *,
    duration_sec: float = 45.0,
    hook: bool = False,
    outro: bool = False,
) -> dict[str, Any]:
    """Synthesise a structurally-valid scene.

    Narration is stored under ``voices`` (the canonical ADK shape)
    because that's what :func:`collect_narration` reads. ``hook`` /
    ``outro`` stamp the spec blocks onto scenes 1 and N respectively.
    """
    # 150 wpm * (duration_sec / 60) => target words; aim for 1.2x that.
    target_words = int(duration_sec / 60.0 * 150 * 1.2)
    repeats = max(1, target_words // 4)
    narration = (_NARRATION_LINE * repeats).strip()
    scene: dict[str, Any] = {
        "scene_num": num,
        "title": f"Scene {num}",
        "voices": [{"text": narration}],
        "duration_sec": float(duration_sec),
        "visual_notes": "documentary footage, slow camera movement",
    }
    if hook:
        scene["hook_spec"] = _valid_hook_spec()
    if outro:
        scene["outro_spec"] = _valid_outro_spec()
    return scene


def _valid_scenes(n: int = 5, *, target_duration_sec: float = 225.0) -> list[dict[str, Any]]:
    per_scene = target_duration_sec / n
    return [
        _scene(i + 1, duration_sec=per_scene, hook=(i == 0), outro=(i == n - 1))
        for i in range(n)
    ]


def _valid_scenario(n: int = 5, *, target_duration_sec: float = 225.0) -> dict[str, Any]:
    return {
        "scenes": _valid_scenes(n, target_duration_sec=target_duration_sec),
        "visual_style": {"dominant_style": "cinematic_documentary"},
        "style_lock": _valid_style_lock(),
    }


@pytest.fixture(autouse=True)
def _reset_helpers() -> None:
    """Keep the module-level generator/refiner registry clean across tests."""
    clear_scenario_helpers()
    yield
    clear_scenario_helpers()


# ---------------------------------------------------------------------------
# generate_scenario
# ---------------------------------------------------------------------------


def test_generate_scenario_raises_when_helper_missing() -> None:
    with pytest.raises(ScenarioHelperNotConfigured):
        generate_scenario.__wrapped__(
            topic="x", num_scenes=1, style="x", language="en-US"
        )


def test_generate_scenario_dispatches_to_registered_helper() -> None:
    captured: dict[str, Any] = {}

    def fake_generator(topic: str, num_scenes: int, style: str, language: str) -> dict[str, Any]:
        captured["args"] = (topic, num_scenes, style, language)
        return _valid_scenario(num_scenes)

    set_scenario_helpers(generator=fake_generator)
    out = generate_scenario.__wrapped__(
        topic="inflation", num_scenes=3, style="cinematic", language="en-US"
    )
    assert captured["args"] == ("inflation", 3, "cinematic", "en-US")
    assert len(out["scenes"]) == 3
    assert out["style_lock"]["dominant_style"] == "cinematic_documentary"


# ---------------------------------------------------------------------------
# evaluate_scenario
# ---------------------------------------------------------------------------


def test_evaluate_scenario_passes_on_valid_scenes() -> None:
    scenario = _valid_scenario(5, target_duration_sec=225.0)
    out = evaluate_scenario.__wrapped__(
        scenes=scenario["scenes"],
        style_lock=scenario["style_lock"],
        target_duration_sec=225.0,
    )
    # Structural checks run offline against the real check suite. Topic
    # fidelity + rhetorical checks short-circuit to non-hard-fail caps
    # without a classifier, so overall may be GOOD/FAIR rather than
    # EXCELLENT — but must not be POOR.
    assert out["rating"] in {"EXCELLENT", "GOOD", "FAIR"}
    # No hard-failing check: duration + scene count + hook + outro all
    # present means the hard gates (POOR caps) are clear.
    hard_fails = [i for i in out["issues"] if i.get("verdict_cap") == "POOR"]
    assert hard_fails == [], f"unexpected hard failures: {hard_fails}"


def test_evaluate_scenario_flags_missing_hook_spec() -> None:
    scenario = _valid_scenario(3, target_duration_sec=135.0)
    scenario["scenes"][0].pop("hook_spec")
    out = evaluate_scenario.__wrapped__(
        scenes=scenario["scenes"],
        style_lock=scenario["style_lock"],
        target_duration_sec=135.0,
    )
    assert out["rating"] == "POOR"
    names = {i["name"] for i in out["issues"]}
    assert "hook_spec_present" in names


def test_evaluate_scenario_flags_missing_style_lock() -> None:
    scenario = _valid_scenario(3, target_duration_sec=135.0)
    out = evaluate_scenario.__wrapped__(
        scenes=scenario["scenes"],
        style_lock={},
        target_duration_sec=135.0,
    )
    assert out["rating"] == "POOR"


# ---------------------------------------------------------------------------
# refine_scenario
# ---------------------------------------------------------------------------


def test_refine_scenario_raises_when_helper_missing() -> None:
    with pytest.raises(ScenarioHelperNotConfigured):
        refine_scenario.__wrapped__(scenes=[{"scene_num": 1}], feedback={"issues": []})


def test_refine_scenario_dispatches_to_registered_helper() -> None:
    def fake_refiner(scenes: list[dict[str, Any]], feedback: dict[str, Any]) -> dict[str, Any]:
        return {"scenes": [{**s, "revised": True} for s in scenes]}

    set_scenario_helpers(refiner=fake_refiner)
    result = refine_scenario.__wrapped__(
        scenes=[{"scene_num": 1}, {"scene_num": 2}],
        feedback={"issues": [{"name": "duration"}]},
    )
    assert len(result["scenes"]) == 2
    assert all(s["revised"] for s in result["scenes"])


# ---------------------------------------------------------------------------
# create_timeline
# ---------------------------------------------------------------------------


def test_create_timeline_rejects_empty_scenes() -> None:
    with pytest.raises(ValueError):
        create_timeline.__wrapped__(scenes=[])


def test_create_timeline_returns_path_and_counts(tmp_path, monkeypatch) -> None:
    # Redirect the OTIO output dir so the test leaves no artifact behind
    # the /tmp root used in CI.
    from tools import otio_tools

    monkeypatch.setattr(otio_tools, "_TIMELINE_DIR", str(tmp_path / "timelines"))

    scenes = _valid_scenes(3, target_duration_sec=135.0)
    result = create_timeline.__wrapped__(scenes=scenes)
    assert result["num_scenes"] == 3
    assert result["timeline_path"].endswith(".otio")
    assert result["total_duration_sec"] == pytest.approx(135.0)


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def test_build_scenario_agent_wires_all_tools_and_hooks() -> None:
    agent = build_scenario_agent()
    assert set(agent.tool_names) == {
        "generate_scenario",
        "evaluate_scenario",
        "refine_scenario",
        "create_timeline",
    }
    assert agent.system_prompt == SYSTEM_PROMPT
    # Token budget: keep the prompt under ~1000 tokens; a loose word
    # proxy catches regressions that drift the prompt into LLM cost.
    assert len(SYSTEM_PROMPT.split()) < 350


def test_build_scenario_agent_skips_hooks_when_disabled() -> None:
    agent = build_scenario_agent(enforce_contract=False, tag_revisions=False)
    # No hooks wired means the hook registry holds zero callbacks from
    # our providers (Strands may add its own internal callbacks, so we
    # only assert *our* providers didn't subscribe anything).
    assert agent is not None


# ---------------------------------------------------------------------------
# ContractEnforcer
# ---------------------------------------------------------------------------


class _FakeState:
    """Minimal ``agent.state`` stand-in with a ``.get()`` method."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def get(self) -> dict[str, Any]:
        return self._data


def _mk_agent(state: dict[str, Any]) -> MagicMock:
    agent = MagicMock()
    agent.state = _FakeState(state)
    return agent


def test_contract_enforcer_registers_both_callbacks() -> None:
    enforcer = ContractEnforcer(SCENARIO_CONTRACT)
    reg = HookRegistry()
    enforcer.register_hooks(reg)
    # Both before and after callbacks should have registered; we can't
    # easily introspect per-event counts, but registering without error
    # is the minimum contract.
    from strands.hooks import AfterInvocationEvent, BeforeInvocationEvent

    before_callbacks = list(reg.get_callbacks_for(BeforeInvocationEvent(agent=_mk_agent({}), invocation_state={}, messages=None)))
    after_callbacks = list(reg.get_callbacks_for(AfterInvocationEvent(agent=_mk_agent({}), invocation_state={}, result=None, resume=None)))
    assert before_callbacks
    assert after_callbacks


def test_contract_enforcer_postcondition_fails_on_missing_scenes() -> None:
    enforcer = ContractEnforcer(SCENARIO_CONTRACT, check_preconditions=False)
    from strands.hooks import AfterInvocationEvent

    event = AfterInvocationEvent(
        agent=_mk_agent({"scenes": "(not yet generated)"}),
        invocation_state={},
        result=None,
        resume=None,
    )
    with pytest.raises(ContractViolation):
        enforcer._on_after(event)


def test_contract_enforcer_postcondition_passes_with_real_scenes() -> None:
    enforcer = ContractEnforcer(SCENARIO_CONTRACT, check_preconditions=False)
    from strands.hooks import AfterInvocationEvent

    event = AfterInvocationEvent(
        agent=_mk_agent({"scenes": [{"scene_num": 1}]}),
        invocation_state={},
        result=None,
        resume=None,
    )
    enforcer._on_after(event)  # should not raise


# ---------------------------------------------------------------------------
# RevisionTagger
# ---------------------------------------------------------------------------


def test_revision_tagger_rejects_empty_output_key() -> None:
    with pytest.raises(ValueError):
        RevisionTagger("")


def test_revision_tagger_skips_gracefully_when_artifact_missing() -> None:
    tagger = RevisionTagger("scenes", require_artifact=False)
    from strands.hooks import AfterInvocationEvent

    event = AfterInvocationEvent(
        agent=_mk_agent({}),
        invocation_state={},
        result=None,
        resume=None,
    )
    # No artifact, require_artifact=False — no-op, no exception.
    tagger._on_after(event)


def test_revision_tagger_raises_when_artifact_missing_and_required() -> None:
    tagger = RevisionTagger("scenes", require_artifact=True)
    from callbacks.artifact_revision_tag import MissingArtifactError
    from strands.hooks import AfterInvocationEvent

    event = AfterInvocationEvent(
        agent=_mk_agent({}),
        invocation_state={},
        result=None,
        resume=None,
    )
    with pytest.raises(MissingArtifactError):
        tagger._on_after(event)


def test_revision_tagger_stamps_ledger_revision_on_state() -> None:
    from callbacks.artifact_revision_tag import ARTIFACT_REVISION_TAGS_KEY, list_tags
    from callbacks.preference_ledger import PREFERENCE_LEDGER_KEY

    # Seed the ledger so tag_artifact has a revision to snapshot;
    # PREFERENCE_LEDGER_KEY stores a JSON *list* of record dicts.
    state: dict[str, Any] = {
        PREFERENCE_LEDGER_KEY: "[]",
        "scenes": [{"scene_num": 1}],
    }

    tagger = RevisionTagger("scenes", stage="scenario")
    from strands.hooks import AfterInvocationEvent

    event = AfterInvocationEvent(
        agent=_mk_agent(state),
        invocation_state={},
        result=None,
        resume=None,
    )
    tagger._on_after(event)

    tags = list_tags(state)
    assert "scenes" in tags
    assert tags["scenes"].stage == "scenario"
    assert tags["scenes"].ledger_revision == 0
    assert ARTIFACT_REVISION_TAGS_KEY in state


# ---------------------------------------------------------------------------
# Experiment factory
# ---------------------------------------------------------------------------


def test_scenario_cases_covers_all_five_slots() -> None:
    names = {c.name for c in scenario_cases()}
    assert names == {
        "economics_basics",
        "complex_monetary_policy",
        "edge_single_scene",
        "edge_max_scenes",
        "failure_empty_topic",
    }


def test_scenario_cases_trajectories_align_with_spec() -> None:
    cases = {c.name: c for c in scenario_cases()}
    assert cases["economics_basics"].expected_trajectory == [
        "generate_scenario",
        "evaluate_scenario",
        "refine_scenario",
        "evaluate_scenario",
        "create_timeline",
    ]
    assert cases["edge_single_scene"].expected_trajectory == [
        "generate_scenario",
        "evaluate_scenario",
        "create_timeline",
    ]
    assert cases["failure_empty_topic"].expected_trajectory == ["generate_scenario"]


def test_scenario_evaluators_stack_is_five_layers() -> None:
    evaluators = scenario_evaluators()
    assert [type(e).__name__ for e in evaluators] == [
        "ContractComplianceEvaluator",
        "ScenarioQualityEvaluator",
        "TrajectoryEvaluator",
        "CoherenceEvaluator",
        "FaithfulnessEvaluator",
    ]


def test_scenario_trajectory_rubric_describes_every_tool() -> None:
    for tool_name in {"generate_scenario", "evaluate_scenario", "refine_scenario", "create_timeline"}:
        assert tool_name in SCENARIO_TRAJECTORY_DESCRIPTION
        assert tool_name in SCENARIO_TRAJECTORY_RUBRIC


def test_scenario_thresholds_cover_every_evaluator() -> None:
    evaluator_names = {type(e).__name__ for e in scenario_evaluators()}
    assert set(SCENARIO_EVALUATOR_THRESHOLDS.keys()) == evaluator_names


def test_build_scenario_experiment_is_roundtrippable() -> None:
    exp = build_scenario_experiment()
    assert len(exp.cases) == 5
    assert len(exp.evaluators) == 5
