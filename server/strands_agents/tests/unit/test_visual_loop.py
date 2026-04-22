"""Unit tests for component 09 — visual-loop SubAgent + trajectory evaluator.

The tests exercise the evaluator with synthetic trajectories (small
focussed regressions for each invariant) and then round-trip every
canonical case shipped by
``strands_agents.evals.experiments.visual_loop`` through the evaluator
stack.
"""

from __future__ import annotations

from typing import Any

from strands_evals.types.evaluation import EvaluationData

from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
    VisualLoopTrajectoryEvaluator,
)
from strands_agents.evals.experiments.visual_loop import (
    VISUAL_LOOP_EVALUATOR_THRESHOLDS,
    build_visual_loop_experiment,
    visual_loop_cases,
    visual_loop_evaluators,
)
from strands_agents.subagents import (
    VISUAL_LOOP_BOOTSTRAP_TOOLS,
    VISUAL_LOOP_MAX_ITERATIONS,
    VISUAL_SUBAGENT_PROMPT,
    VISUAL_SUBAGENT_TOOL_NAMES,
    VISUAL_SUBAGENT_TOOLS,
    build_visual_subagent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract(scene_num: int, turn: int) -> dict[str, Any]:
    return {
        "name": "extract_phrases",
        "at_turn": turn,
        "args": {"scene_num": scene_num},
    }


def _validate(turn: int) -> dict[str, Any]:
    return {"name": "validate_phrases", "at_turn": turn, "args": {}}


def _persist_analysis(turn: int) -> dict[str, Any]:
    return {"name": "persist_content_analysis", "at_turn": turn, "args": {}}


def _propose(
    scene_num: int, phrase_idx: int, turn: int, revision: int = 1
) -> dict[str, Any]:
    return {
        "name": "propose_concept",
        "at_turn": turn,
        "args": {
            "scene_num": scene_num,
            "phrase_idx": phrase_idx,
            "revision": revision,
        },
    }


def _check_style(turn: int) -> dict[str, Any]:
    return {"name": "check_style_lock", "at_turn": turn, "args": {}}


def _persist_concepts(turn: int) -> dict[str, Any]:
    return {"name": "persist_visual_concepts", "at_turn": turn, "args": {}}


def _score(turn: int, rating: str = "GOOD") -> dict[str, Any]:
    return {
        "name": "score_visual_coherence",
        "at_turn": turn,
        "args": {"expected_rating": rating},
    }


def _persist_report(turn: int) -> dict[str, Any]:
    return {"name": "persist_coherence_report", "at_turn": turn, "args": {}}


def _delegate(turn: int, reason: str = "escalate") -> dict[str, Any]:
    return {
        "name": "task",
        "at_turn": turn,
        "args": {"subagent_type": "escalation", "description": reason},
    }


def _case(
    trajectory: list[dict[str, Any]],
    **metadata: Any,
) -> EvaluationData[Any, Any]:
    return EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata=metadata,
    )


def _get(outputs: list[Any], label: str) -> Any:
    return next(o for o in outputs if o.label == label)


# ---------------------------------------------------------------------------
# SubAgent spec invariants
# ---------------------------------------------------------------------------


def test_subagent_spec_is_well_formed() -> None:
    spec = build_visual_subagent()
    assert spec["name"] == "visual"
    assert "visual production planner" in spec["description"].lower()
    assert spec["system_prompt"] == VISUAL_SUBAGENT_PROMPT
    assert tuple(spec["tools"]) == tuple(VISUAL_SUBAGENT_TOOLS)
    assert isinstance(spec["model"], str) and spec["model"]


def test_subagent_prompt_forbids_launch_calls() -> None:
    assert "MUST NOT call any launch_* tool" in VISUAL_SUBAGENT_PROMPT


def test_subagent_prompt_describes_full_loop() -> None:
    for name in VISUAL_SUBAGENT_TOOL_NAMES:
        assert name in VISUAL_SUBAGENT_PROMPT, f"prompt omits {name}"


def test_subagent_tool_names_match_declared_tools() -> None:
    declared = tuple(t.tool_name for t in VISUAL_SUBAGENT_TOOLS)
    assert declared == VISUAL_SUBAGENT_TOOL_NAMES


def test_subagent_model_env_override(monkeypatch: Any) -> None:
    monkeypatch.setenv("STRANDS_THINKER_MODEL", "openai/gpt-5-preview")
    spec = build_visual_subagent()
    assert spec["model"] == "openai/gpt-5-preview"


def test_subagent_model_explicit_override() -> None:
    spec = build_visual_subagent(model="anthropic/claude-sonnet-4")
    assert spec["model"] == "anthropic/claude-sonnet-4"


def test_subagent_extra_tools_appended() -> None:
    def sentinel() -> dict[str, Any]:
        return {}

    spec = build_visual_subagent(extra_tools=(sentinel,))
    assert spec["tools"][-1] is sentinel
    assert len(spec["tools"]) == len(VISUAL_SUBAGENT_TOOLS) + 1


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_missing_expected_iterations_flags_missing_config() -> None:
    outputs = VisualLoopTrajectoryEvaluator().evaluate(
        _case([_extract(1, 1)], expected_scene_count=3)
    )
    assert len(outputs) == 1
    assert outputs[0].test_pass is False
    assert outputs[0].label == "visual_loop.missing_config"


def test_missing_scene_count_flags_missing_config() -> None:
    outputs = VisualLoopTrajectoryEvaluator().evaluate(
        _case([_extract(1, 1)], expected_iterations=1)
    )
    assert outputs[0].test_pass is False
    assert outputs[0].label == "visual_loop.missing_config"


def test_revision_counts_length_mismatch_rejected() -> None:
    outputs = VisualLoopTrajectoryEvaluator().evaluate(
        _case(
            [_extract(1, 1)],
            expected_iterations=2,
            expected_scene_count=3,
            expected_revision_counts=[],
        )
    )
    assert outputs[0].test_pass is False
    assert outputs[0].label == "visual_loop.missing_config"


def test_non_list_trajectory_flags_missing_actual() -> None:
    class _Stub:
        actual_trajectory: Any = "not a list"
        metadata: dict[str, Any] = {
            "expected_iterations": 1,
            "expected_scene_count": 3,
        }

    outputs = VisualLoopTrajectoryEvaluator().evaluate(_Stub())  # type: ignore[arg-type]
    assert outputs[0].test_pass is False
    assert outputs[0].label == "visual_loop.missing_actual"


# ---------------------------------------------------------------------------
# One-shot happy path
# ---------------------------------------------------------------------------


def _one_shot_trajectory(n: int = 3) -> list[dict[str, Any]]:
    turn = 1
    trajectory: list[dict[str, Any]] = []
    for i in range(n):
        trajectory.append(_extract(i + 1, turn))
    turn += 1
    trajectory.append(_validate(turn))
    turn += 1
    trajectory.append(_persist_analysis(turn))
    turn += 1
    for i in range(n):
        trajectory.append(_propose(i + 1, 0, turn))
    turn += 1
    trajectory.append(_check_style(turn))
    turn += 1
    trajectory.append(_persist_concepts(turn))
    turn += 1
    trajectory.append(_score(turn, rating="GOOD"))
    turn += 1
    trajectory.append(_persist_report(turn))
    return trajectory


def test_one_shot_passes_every_evaluator_output() -> None:
    outputs = VisualLoopTrajectoryEvaluator().evaluate(
        _case(
            _one_shot_trajectory(3),
            expected_iterations=1,
            expected_scene_count=3,
            expected_revision_counts=[],
        )
    )
    for output in outputs:
        assert output.test_pass is True, output.reason
    assert _get(outputs, "visual_loop.iteration_count").score == 1.0
    assert _get(outputs, "visual_loop.shape").score == 1.0
    assert _get(outputs, "visual_loop.bootstrap_once").score == 1.0
    assert _get(outputs, "visual_loop.revision_scope").score == 1.0
    assert _get(outputs, "visual_loop.forbidden_launch").score == 1.0
    assert _get(outputs, "visual_loop.delegation").score == 1.0


# ---------------------------------------------------------------------------
# Shape / bootstrap violations
# ---------------------------------------------------------------------------


def test_missing_validate_phrases_fails_shape() -> None:
    trajectory = _one_shot_trajectory(3)
    trajectory = [
        call for call in trajectory if call["name"] != "validate_phrases"
    ]
    outputs = VisualLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=1,
            expected_scene_count=3,
            expected_revision_counts=[],
        )
    )
    # validate_phrases is a bootstrap tool; dropping it reduces the
    # iteration-1 bootstrap count from expected (scene_count + 2) to
    # (scene_count + 1), failing the bootstrap_once gate.
    assert _get(outputs, "visual_loop.bootstrap_once").test_pass is False


def test_score_before_persist_concepts_fails_shape() -> None:
    # Swap persist_visual_concepts and score_visual_coherence so the
    # concepts aren't persisted before scoring.
    trajectory = [
        _extract(1, 1),
        _validate(2),
        _persist_analysis(3),
        _propose(1, 0, 4),
        _check_style(5),
        _score(6, rating="GOOD"),  # score before persist_concepts
        _persist_concepts(7),
        _persist_report(8),
    ]
    outputs = VisualLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=1,
            expected_scene_count=1,
            expected_revision_counts=[],
        )
    )
    assert _get(outputs, "visual_loop.shape").test_pass is False


def test_bootstrap_in_revision_iteration_fails() -> None:
    trajectory = _one_shot_trajectory(3) + [
        # Iteration 2 illegally re-runs extract_phrases. Bootstrap
        # tools must appear only in iteration 1.
        _extract(1, 9),
        _propose(1, 0, 10, revision=2),
        _check_style(11),
        _persist_concepts(12),
        _score(13, rating="GOOD"),
        _persist_report(14),
    ]
    outputs = VisualLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=2,
            expected_scene_count=3,
            expected_revision_counts=[1],
        )
    )
    assert _get(outputs, "visual_loop.bootstrap_once").test_pass is False


# ---------------------------------------------------------------------------
# Revision scope
# ---------------------------------------------------------------------------


def test_correct_revision_scope() -> None:
    trajectory = _one_shot_trajectory(5) + [
        _propose(2, 0, 9, revision=2),
        _propose(4, 0, 9, revision=2),
        _check_style(10),
        _persist_concepts(11),
        _score(12, rating="GOOD"),
        _persist_report(13),
    ]
    outputs = VisualLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=2,
            expected_scene_count=5,
            expected_revision_counts=[2],
        )
    )
    for output in outputs:
        assert output.test_pass is True, output.reason


def test_wrong_revision_scope_fails() -> None:
    # Re-propose every scene on iteration 2 — too broad. revision_scope
    # must flag this.
    trajectory = _one_shot_trajectory(3) + [
        _propose(1, 0, 9, revision=2),
        _propose(2, 0, 9, revision=2),
        _propose(3, 0, 9, revision=2),
        _check_style(10),
        _persist_concepts(11),
        _score(12, rating="GOOD"),
        _persist_report(13),
    ]
    outputs = VisualLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=2,
            expected_scene_count=3,
            expected_revision_counts=[1],
        )
    )
    assert _get(outputs, "visual_loop.revision_scope").test_pass is False


# ---------------------------------------------------------------------------
# Cap + delegation
# ---------------------------------------------------------------------------


def test_iteration_cap_without_delegation_fails() -> None:
    trajectory = _one_shot_trajectory(3)
    turn = 9
    for iteration in range(2, VISUAL_LOOP_MAX_ITERATIONS + 1):
        trajectory.extend(
            [
                _propose(1, 0, turn, revision=iteration),
                _check_style(turn + 1),
                _persist_concepts(turn + 2),
                _score(turn + 3, rating="FAIR"),
                _persist_report(turn + 4),
            ]
        )
        turn += 5
    outputs = VisualLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=VISUAL_LOOP_MAX_ITERATIONS,
            expected_scene_count=3,
            expected_revision_counts=[1] * (VISUAL_LOOP_MAX_ITERATIONS - 1),
            expects_pass=False,
            expects_delegation=True,
        )
    )
    # Cap reached; delegation missing → delegation gate fails.
    assert _get(outputs, "visual_loop.delegation").test_pass is False


def test_forbidden_launch_detected() -> None:
    trajectory = _one_shot_trajectory(3) + [
        {"name": "launch_video_render", "at_turn": 9, "args": {"scene_id": "1"}},
    ]
    outputs = VisualLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=1,
            expected_scene_count=3,
            expected_revision_counts=[],
        )
    )
    assert _get(outputs, "visual_loop.forbidden_launch").test_pass is False


def test_neutral_tools_ignored() -> None:
    trajectory = [
        {"name": "write_todos", "at_turn": 0, "args": {}},
        *_one_shot_trajectory(3),
        {"name": "write_file", "at_turn": 9, "args": {"path": "scratch.md"}},
    ]
    outputs = VisualLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=1,
            expected_scene_count=3,
            expected_revision_counts=[],
        )
    )
    for output in outputs:
        assert output.test_pass is True, output.reason


# ---------------------------------------------------------------------------
# Experiment factory coverage
# ---------------------------------------------------------------------------


def test_experiment_has_five_cases_and_two_evaluators() -> None:
    experiment = build_visual_loop_experiment()
    assert len(experiment.cases) == 5
    assert {c.name for c in experiment.cases} == {
        "one_shot_good",
        "one_revise",
        "persistent_fair",
        "analyst_fails",
        "style_lock_drift",
    }
    assert len(experiment.evaluators) == 2


def test_experiment_thresholds_cover_every_evaluator() -> None:
    evaluator_names = {type(e).__name__ for e in visual_loop_evaluators()}
    threshold_names = set(VISUAL_LOOP_EVALUATOR_THRESHOLDS.keys())
    assert evaluator_names == threshold_names


def test_every_case_passes_trajectory_evaluator() -> None:
    evaluator = VisualLoopTrajectoryEvaluator()
    for case in visual_loop_cases():
        assert case.expected_trajectory is not None
        data = EvaluationData[Any, Any](
            input=case.input,
            actual_trajectory=case.expected_trajectory,
            metadata=case.metadata,
        )
        outputs = evaluator.evaluate(data)
        for output in outputs:
            assert output.test_pass is True, (
                f"case={case.name} failed {output.label}: {output.reason}"
            )


def test_every_non_failure_case_passes_contract_evaluator() -> None:
    """Contract compliance is a hard gate except for the failure case.

    ``analyst_fails`` intentionally short-circuits before
    ``persist_visual_concepts`` runs — the contract MUST fail on it to
    prove the check actually detects missing output. The other four
    cases populate the final state for the visual stage and MUST
    satisfy the contract.
    """
    evaluator = next(
        e
        for e in visual_loop_evaluators()
        if isinstance(e, ContractComplianceEvaluator)
    )
    for case in visual_loop_cases():
        metadata = case.metadata or {}
        final_state = metadata.get("final_state", {})
        data = EvaluationData[Any, Any](
            input=case.input,
            actual_output=final_state,
            actual_trajectory=case.expected_trajectory,
            metadata=metadata,
        )
        outputs = evaluator.evaluate(data)
        every_pass = all(o.test_pass for o in outputs)
        if case.name == "analyst_fails":
            assert not every_pass, (
                "analyst_fails case should fail contract compliance"
            )
        else:
            assert every_pass, (
                f"case={case.name} failed contract compliance: "
                + ", ".join(o.reason for o in outputs if not o.test_pass)
            )


def test_bootstrap_tool_set_is_exactly_the_fixed_analysis_tools() -> None:
    assert VISUAL_LOOP_BOOTSTRAP_TOOLS == frozenset(
        {"extract_phrases", "validate_phrases", "persist_content_analysis"}
    )


def test_max_iterations_constant() -> None:
    assert VISUAL_LOOP_MAX_ITERATIONS == 5
