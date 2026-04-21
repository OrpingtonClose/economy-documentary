"""Unit tests for component 05 — timing-loop trajectory evaluator.

The evaluator is exercised both directly (synthetic trajectories built
in-file) and through the canonical experiment factory
(``strands_agents.evals.experiments.timing_loop``) so that the cases
shipped as specification also pass every hard-gate check.
"""

from __future__ import annotations

from typing import Any

from strands_evals.types.evaluation import EvaluationData

from strands_agents.evals.evaluators import TimingLoopTrajectoryEvaluator
from strands_agents.evals.experiments.timing_loop import (
    TIMING_LOOP_EVALUATOR_THRESHOLDS,
    build_timing_loop_experiment,
    timing_loop_cases,
    timing_loop_evaluators,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _launch(scene: str, turn: int) -> dict[str, Any]:
    return {"name": "launch_audio_render", "at_turn": turn, "args": {"scene_id": scene}}


def _await(task_ids: list[str], turn: int) -> dict[str, Any]:
    return {"name": "await_tasks", "at_turn": turn, "args": {"task_ids": task_ids}}


def _evaluate(turn: int, *, intent_sec: float = 60.0) -> dict[str, Any]:
    return {
        "name": "evaluate_timing",
        "at_turn": turn,
        "args": {"intent_target_sec": intent_sec},
    }


def _refine(turn: int, *, timing_report: dict[str, Any] | None) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if timing_report is not None:
        args["timing_report"] = timing_report
    return {"name": "refine_scenario", "at_turn": turn, "args": args}


def _delegate(turn: int) -> dict[str, Any]:
    return {
        "name": "task",
        "at_turn": turn,
        "args": {"subagent_type": "escalation", "description": "cap reached"},
    }


def _case(trajectory: list[dict[str, Any]], **metadata: Any) -> EvaluationData[Any, Any]:
    return EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata=metadata,
    )


def _get(outputs: list[Any], label: str) -> Any:
    return next(o for o in outputs if o.label == label)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_missing_expected_iterations_fails() -> None:
    evaluator = TimingLoopTrajectoryEvaluator()
    outputs = evaluator.evaluate(_case([_launch("s1", 1)]))
    assert len(outputs) == 1
    assert outputs[0].test_pass is False
    assert outputs[0].label == "timing_loop.missing_config"


def test_zero_expected_iterations_rejected() -> None:
    evaluator = TimingLoopTrajectoryEvaluator()
    outputs = evaluator.evaluate(_case([_launch("s1", 1)], expected_iterations=0))
    assert outputs[0].test_pass is False
    assert outputs[0].label == "timing_loop.missing_config"


def test_missing_trajectory_surfaces_missing_actual() -> None:
    evaluator = TimingLoopTrajectoryEvaluator()
    # ``EvaluationData`` pydantic-validates ``actual_trajectory`` to a
    # list, so we bypass construction and invoke ``evaluate`` on a
    # stub carrying a non-list value — proving the evaluator would
    # fail closed if a future EvaluationData release relaxed the
    # validator.
    class _Stub:
        actual_trajectory: Any = "not a list"
        metadata = {"expected_iterations": 1}

    outputs = evaluator.evaluate(_Stub())  # type: ignore[arg-type]
    assert outputs[0].test_pass is False
    assert outputs[0].label == "timing_loop.missing_actual"


# ---------------------------------------------------------------------------
# One-shot pass
# ---------------------------------------------------------------------------


def _one_shot_trajectory() -> list[dict[str, Any]]:
    return [
        _launch("s1", 1),
        _launch("s2", 1),
        _launch("s3", 1),
        _await(["t1", "t2", "t3"], 2),
        _evaluate(3),
    ]


def test_one_shot_pass() -> None:
    evaluator = TimingLoopTrajectoryEvaluator()
    outputs = evaluator.evaluate(
        _case(
            _one_shot_trajectory(),
            expected_iterations=1,
            expected_refines=0,
            expects_pass=True,
            expects_delegation=False,
        )
    )
    for output in outputs:
        assert output.test_pass is True, output.reason
    assert _get(outputs, "timing_loop.iteration_count").score == 1.0
    assert _get(outputs, "timing_loop.shape").score == 1.0
    assert _get(outputs, "timing_loop.refine_count").score == 1.0
    assert _get(outputs, "timing_loop.delegation").score == 1.0


def test_one_shot_refine_count_defaults_to_zero_when_pass_expected() -> None:
    evaluator = TimingLoopTrajectoryEvaluator()
    # expected_refines omitted → default = expected_iterations - 1 = 0.
    outputs = evaluator.evaluate(
        _case(_one_shot_trajectory(), expected_iterations=1)
    )
    assert _get(outputs, "timing_loop.refine_count").test_pass is True


# ---------------------------------------------------------------------------
# Two-iteration pass with one refine
# ---------------------------------------------------------------------------


def test_two_iteration_one_refine_happy_path() -> None:
    trajectory = [
        _launch("s1", 1),
        _launch("s2", 1),
        _await(["t1", "t2"], 2),
        _evaluate(3),
        _refine(4, timing_report={"status": "FAIL", "per_scene": [{"scene_num": 2, "delta_sec": 6.0}]}),
        {"name": "write_file", "at_turn": 4, "args": {"path": "scenes.json"}},
        _launch("s1", 5),
        _launch("s2", 5),
        _await(["t3", "t4"], 6),
        _evaluate(7),
    ]
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(trajectory, expected_iterations=2, expected_refines=1, expects_pass=True)
    )
    for o in outputs:
        assert o.test_pass is True, o.reason


# ---------------------------------------------------------------------------
# Shape violations
# ---------------------------------------------------------------------------


def test_missing_await_between_launch_and_evaluate_fails_shape() -> None:
    trajectory = [
        _launch("s1", 1),
        _launch("s2", 1),
        _evaluate(2),  # skipped await_tasks
    ]
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(trajectory, expected_iterations=1)
    )
    assert _get(outputs, "timing_loop.shape").test_pass is False


def test_await_before_launch_fails_shape() -> None:
    trajectory = [
        _await(["t1"], 1),  # nothing launched yet
        _launch("s1", 2),
        _evaluate(3),
    ]
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(trajectory, expected_iterations=1)
    )
    assert _get(outputs, "timing_loop.shape").test_pass is False


def test_second_await_in_iteration_fails_shape() -> None:
    trajectory = [
        _launch("s1", 1),
        _await(["t1"], 2),
        _await(["t1"], 2),  # duplicate await in same iteration
        _evaluate(3),
    ]
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(trajectory, expected_iterations=1)
    )
    assert _get(outputs, "timing_loop.shape").test_pass is False


def test_double_await_then_next_iteration_counts_as_two_iterations() -> None:
    """Double-await in iteration 1 must not collapse two iterations into one.

    Regression guard: the ``evaluate_timing`` branch in
    :func:`_split_iterations` must advance the state to ``"evaluated"``
    regardless of prior state so the next ``launch_audio_render``
    correctly closes the iteration. Otherwise the iteration counter
    under-reports by 1 and hides the shape violation.
    """
    trajectory = [
        _launch("s1", 1),
        _await(["t1"], 2),
        _await(["t1"], 2),  # duplicate await in iteration 1
        _evaluate(3),
        _launch("s1", 4),
        _await(["t2"], 5),
        _evaluate(6),
    ]
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(trajectory, expected_iterations=2, expected_refines=0, expects_pass=True)
    )
    # Both iterations counted, and the first-iteration shape violation
    # still surfaces.
    assert _get(outputs, "timing_loop.iteration_count").test_pass is True
    assert _get(outputs, "timing_loop.shape").test_pass is False


def test_unfinished_trailing_iteration_fails_shape() -> None:
    trajectory = [
        _launch("s1", 1),
        _launch("s2", 1),
        _await(["t1", "t2"], 2),
        _evaluate(3),
        _refine(4, timing_report={"status": "FAIL"}),
        _launch("s1", 5),
        # missing await_tasks + evaluate_timing for iteration 2
    ]
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(trajectory, expected_iterations=2, expected_refines=1, expects_pass=True)
    )
    assert _get(outputs, "timing_loop.shape").test_pass is False


# ---------------------------------------------------------------------------
# Refine count / payload
# ---------------------------------------------------------------------------


def test_refine_count_mismatch_fails() -> None:
    trajectory = [
        _launch("s1", 1),
        _await(["t1"], 2),
        _evaluate(3),
        _refine(4, timing_report={"status": "FAIL"}),
        _refine(4, timing_report={"status": "FAIL"}),  # double refine in same iteration
        _launch("s1", 5),
        _await(["t2"], 6),
        _evaluate(7),
    ]
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(trajectory, expected_iterations=2, expected_refines=1, expects_pass=True)
    )
    assert _get(outputs, "timing_loop.refine_count").test_pass is False


def test_refine_without_timing_report_fails_inputs() -> None:
    trajectory = [
        _launch("s1", 1),
        _await(["t1"], 2),
        _evaluate(3),
        _refine(4, timing_report=None),
        _launch("s1", 5),
        _await(["t2"], 6),
        _evaluate(7),
    ]
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(trajectory, expected_iterations=2, expected_refines=1, expects_pass=True)
    )
    # refine_count still passes (one refine, one expected), but
    # refine_inputs must flag the missing report.
    assert _get(outputs, "timing_loop.refine_count").test_pass is True
    assert _get(outputs, "timing_loop.refine_inputs").test_pass is False


def test_no_refine_emits_no_refine_inputs_output() -> None:
    # When no refine calls are present, the refine_inputs check is
    # skipped — we only emit 4 outputs (count / shape / refine_count /
    # delegation).
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(_one_shot_trajectory(), expected_iterations=1, expected_refines=0)
    )
    labels = {o.label for o in outputs}
    assert "timing_loop.refine_inputs" not in labels


# ---------------------------------------------------------------------------
# Iteration cap + delegation
# ---------------------------------------------------------------------------


def _loop_iteration(turn_start: int, *, iteration: int) -> tuple[list[dict[str, Any]], int]:
    turn = turn_start
    calls = [
        _launch("s1", turn),
        _launch("s2", turn),
    ]
    turn += 1
    calls.append(_await([f"t{iteration}_s1", f"t{iteration}_s2"], turn))
    turn += 1
    calls.append(_evaluate(turn))
    turn += 1
    calls.append(_refine(turn, timing_report={"status": "FAIL", "iteration": iteration}))
    turn += 1
    return calls, turn


def test_iteration_cap_with_delegation_passes() -> None:
    trajectory: list[dict[str, Any]] = []
    turn = 1
    for iteration in range(10):
        chunk, turn = _loop_iteration(turn, iteration=iteration)
        trajectory.extend(chunk)
    trajectory.append(_delegate(turn))
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=10,
            expected_refines=10,
            expects_pass=False,
            expects_delegation=True,
        )
    )
    for o in outputs:
        assert o.test_pass is True, o.reason


def test_missing_delegation_when_expected_fails() -> None:
    trajectory: list[dict[str, Any]] = []
    turn = 1
    for iteration in range(10):
        chunk, turn = _loop_iteration(turn, iteration=iteration)
        trajectory.extend(chunk)
    # forgot to delegate to escalation
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=10,
            expected_refines=10,
            expects_pass=False,
            expects_delegation=True,
        )
    )
    assert _get(outputs, "timing_loop.delegation").test_pass is False


def test_unexpected_delegation_fails() -> None:
    trajectory = _one_shot_trajectory() + [_delegate(4)]
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=1,
            expected_refines=0,
            expects_pass=True,
            expects_delegation=False,
        )
    )
    assert _get(outputs, "timing_loop.delegation").test_pass is False


def test_delegate_to_escalation_tool_name_accepted() -> None:
    trajectory = _one_shot_trajectory() + [
        {"name": "delegate_to_escalation", "at_turn": 4, "args": {}}
    ]
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=1,
            expected_refines=0,
            expects_pass=True,
            expects_delegation=True,
        )
    )
    assert _get(outputs, "timing_loop.delegation").test_pass is True


def test_iteration_cap_exceeded_fails_count() -> None:
    # Build 11 iterations — exceeds the default max_iterations=10.
    trajectory: list[dict[str, Any]] = []
    turn = 1
    for iteration in range(11):
        chunk, turn = _loop_iteration(turn, iteration=iteration)
        trajectory.extend(chunk)
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=11,  # orchestrator claimed 11
            expected_refines=11,
            expects_pass=False,
            expects_delegation=False,
        )
    )
    # iteration_count evaluator must flag the cap breach even though
    # actual matches expected.
    iteration_out = _get(outputs, "timing_loop.iteration_count")
    assert iteration_out.test_pass is False
    assert "cap 10" in iteration_out.reason


def test_custom_max_iterations_allows_longer_runs() -> None:
    trajectory: list[dict[str, Any]] = []
    turn = 1
    for iteration in range(12):
        chunk, turn = _loop_iteration(turn, iteration=iteration)
        trajectory.extend(chunk)
    trajectory.append(_delegate(turn))
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(
            trajectory,
            expected_iterations=12,
            expected_refines=12,
            expects_pass=False,
            expects_delegation=True,
            max_iterations=15,
        )
    )
    assert _get(outputs, "timing_loop.iteration_count").test_pass is True


# ---------------------------------------------------------------------------
# Ignored tool calls
# ---------------------------------------------------------------------------


def test_write_todos_between_calls_ignored() -> None:
    trajectory = [
        {"name": "write_todos", "at_turn": 0, "args": {}},
        _launch("s1", 1),
        _launch("s2", 1),
        {"name": "write_todos", "at_turn": 1, "args": {}},
        _await(["t1", "t2"], 2),
        _evaluate(3),
    ]
    outputs = TimingLoopTrajectoryEvaluator().evaluate(
        _case(trajectory, expected_iterations=1, expected_refines=0, expects_pass=True)
    )
    for o in outputs:
        assert o.test_pass is True, o.reason


# ---------------------------------------------------------------------------
# Experiment factory coverage
# ---------------------------------------------------------------------------


def test_experiment_has_five_cases_and_three_evaluators() -> None:
    experiment = build_timing_loop_experiment()
    assert len(experiment.cases) == 5
    assert {c.name for c in experiment.cases} == {
        "one_shot_pass",
        "one_refine_pass",
        "per_scene_spike",
        "refiner_no_op",
        "max_iterations",
    }
    assert len(experiment.evaluators) == 3


def test_experiment_thresholds_cover_every_evaluator() -> None:
    evaluator_names = {type(e).__name__ for e in timing_loop_evaluators()}
    threshold_names = set(TIMING_LOOP_EVALUATOR_THRESHOLDS.keys())
    assert evaluator_names == threshold_names


def test_every_experiment_case_passes_trajectory_evaluator() -> None:
    evaluator = TimingLoopTrajectoryEvaluator()
    for case in timing_loop_cases():
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


def test_parallel_launch_evaluator_passes_every_case() -> None:
    from strands_agents.evals.evaluators import ParallelLaunchEvaluator

    evaluator = ParallelLaunchEvaluator()
    # With per-batch evaluator semantics, every case (including the two
    # cap-hit cases that emit 10 batches of parallel launches) passes
    # using the stock expected_count metadata. The evaluator groups
    # launches by ``at_turn`` and asserts every batch matches the
    # per-iteration expected_count.
    for case in timing_loop_cases():
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
