"""Hermetic trajectory proof for Component 05 (timing-loop).

Component 05 is a pure trajectory-shape check — no LLM involved in the
evaluator itself — so the proof is deterministic rather than live.

The three clear-cut shapes we pin here are:

1. One-iteration happy path (launches → await → evaluate, all pass).
2. Two-iteration path that converges after one refine; proves
   ``refine_scenario`` must carry a non-empty ``timing_report`` arg.
3. 10-iteration stuck path that ends with a delegation to the
   escalation SubAgent.  The AGENTS.md hard invariant says the loop
   tops out at 10 iterations; a trajectory that keeps looping past
   that must be rejected.

If any of these shapes regresses (e.g. a double-await slips in, a
refine is called without the report, or delegation at the cap is
dropped), the evaluator returns ``test_pass=False`` and this test
catches it.
"""

from __future__ import annotations

from typing import Any

from strands_evals.types.evaluation import EvaluationData

from strands_agents.evals.evaluators.timing_loop_trajectory import (
    TimingLoopTrajectoryEvaluator,
)


def _call(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args}


_SENTINEL_REPORT = {"violations": ["scene_2 drift"]}


def _iter(
    *,
    scene_count: int = 3,
    refine: bool = False,
    timing_report: dict[str, Any] | None = _SENTINEL_REPORT,
) -> list[dict[str, Any]]:
    """Build one well-shaped timing-loop iteration.

    Args:
        scene_count: Number of parallel ``launch_audio_render`` calls.
        refine: Whether a trailing ``refine_scenario`` call is emitted.
        timing_report: Payload forwarded to the refiner.  Pass ``None``
            to emit a ``refine_scenario`` call with no ``timing_report``
            argument at all — the exact shape the hard-invariant gate
            forbids.
    """
    calls = [_call("launch_audio_render", scene_id=i + 1) for i in range(scene_count)]
    calls.append(_call("await_tasks"))
    calls.append(_call("evaluate_timing"))
    if refine:
        if timing_report is None:
            calls.append({"name": "refine_scenario", "args": {}})
        else:
            calls.append(_call("refine_scenario", timing_report=timing_report))
    return calls


def _outputs_to_map(outputs: list[Any]) -> dict[str, Any]:
    return {o.label: o for o in outputs}


def _case(
    trajectory: list[dict[str, Any]], **metadata: Any
) -> EvaluationData[Any, Any]:
    return EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata=metadata,
    )


def test_single_iteration_happy_path_passes_all_gates() -> None:
    trajectory = _iter(scene_count=3, refine=False)
    case = _case(trajectory, expected_iterations=1, expects_pass=True)
    results = _outputs_to_map(TimingLoopTrajectoryEvaluator().evaluate(case))
    # refine_inputs label is only emitted when >=1 refine happened; the
    # happy path has 0 refines, so we check only the core four gates.
    for label in (
        "timing_loop.iteration_count",
        "timing_loop.shape",
        "timing_loop.refine_count",
        "timing_loop.delegation",
    ):
        assert label in results, f"missing evaluator output {label}"
        assert results[label].test_pass, f"{label} failed: {results[label].reason}"


def test_two_iterations_with_refine_pass_only_when_report_supplied() -> None:
    good_trajectory = _iter(
        scene_count=3, refine=True, timing_report={"violations": ["scene_1"]}
    ) + _iter(scene_count=3, refine=False)
    good_case = _case(good_trajectory, expected_iterations=2, expects_pass=True)
    good_results = _outputs_to_map(TimingLoopTrajectoryEvaluator().evaluate(good_case))
    assert good_results["timing_loop.refine_inputs"].test_pass, (
        f"refine with a valid timing_report rejected: "
        f"{good_results['timing_loop.refine_inputs'].reason}"
    )

    bad_trajectory = _iter(scene_count=3, refine=True, timing_report=None) + _iter(
        scene_count=3, refine=False
    )
    bad_case = _case(bad_trajectory, expected_iterations=2, expects_pass=True)
    bad_results = _outputs_to_map(TimingLoopTrajectoryEvaluator().evaluate(bad_case))
    assert not bad_results["timing_loop.refine_inputs"].test_pass, (
        "refine_scenario without a timing_report was accepted — hard invariant broken"
    )


def test_ten_iterations_without_delegation_is_rejected() -> None:
    """AGENTS.md hard cap: loop must delegate to escalation at iter 10.

    A trajectory that runs 10 failed iterations and then simply stops
    (no delegation) must fail the delegation gate.
    """
    trajectory: list[dict[str, Any]] = []
    for _ in range(10):
        trajectory.extend(
            _iter(
                scene_count=2,
                refine=True,
                timing_report={"violations": ["scene_1"]},
            )
        )
    case = _case(
        trajectory,
        expected_iterations=10,
        expects_pass=False,
        expects_delegation=True,
    )
    results = _outputs_to_map(TimingLoopTrajectoryEvaluator().evaluate(case))
    assert not results["timing_loop.delegation"].test_pass, (
        "10 failed iterations without escalation delegation must fail "
        "the delegation gate"
    )


def test_ten_iterations_with_escalation_delegation_passes() -> None:
    """Same 10-iter path but correctly delegates — all gates pass."""
    trajectory: list[dict[str, Any]] = []
    for _ in range(10):
        trajectory.extend(
            _iter(
                scene_count=2,
                refine=True,
                timing_report={"violations": ["scene_1"]},
            )
        )
    trajectory.append(_call("task", subagent_type="escalation", prompt="loop stuck"))
    case = _case(
        trajectory,
        expected_iterations=10,
        expects_pass=False,
        expects_delegation=True,
    )
    results = _outputs_to_map(TimingLoopTrajectoryEvaluator().evaluate(case))
    for label in (
        "timing_loop.iteration_count",
        "timing_loop.shape",
        "timing_loop.refine_count",
        "timing_loop.refine_inputs",
        "timing_loop.delegation",
    ):
        assert results[label].test_pass, f"{label} should pass: {results[label].reason}"
