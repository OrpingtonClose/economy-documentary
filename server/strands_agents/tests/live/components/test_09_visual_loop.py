"""Hermetic trajectory proof for Component 09 (visual-loop).

Component 09 is a pure trajectory-shape check — no LLM involved in
the evaluator itself — so the proof is deterministic rather than
live.

The four clear-cut shapes pinned here:

1. One-iteration happy path: bootstrap + one scoring iteration with
   every phrase covered.
2. Two-iteration path that revises a subset of phrases (the weak
   scenes) and scores again.
3. Bootstrap-tool-in-iteration-2 regression: if any of
   ``extract_phrases`` / ``validate_phrases`` /
   ``persist_content_analysis`` re-runs on iteration 2, the
   ``bootstrap_once`` gate fails.
4. Hit-the-cap path: 5 failing iterations followed by delegation to
   the escalation SubAgent.  The AGENTS.md hard invariant says the
   loop tops out at 5; a trajectory that keeps looping past that
   without delegating must be rejected.
"""

from __future__ import annotations

from typing import Any

from strands_evals.types.evaluation import EvaluationData

from strands_agents.evals.evaluators.visual_loop_trajectory import (
    VisualLoopTrajectoryEvaluator,
)
from strands_agents.subagents.visual import VISUAL_LOOP_MAX_ITERATIONS


def _call(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args}


def _bootstrap(scene_count: int) -> list[dict[str, Any]]:
    calls = [_call("extract_phrases", scene_num=i + 1) for i in range(scene_count)]
    calls.append(_call("validate_phrases"))
    calls.append(_call("persist_content_analysis"))
    return calls


def _score_block(propose_count: int) -> list[dict[str, Any]]:
    calls = [_call("propose_concept", idx=i) for i in range(propose_count)]
    calls.append(_call("check_style_lock"))
    calls.append(_call("persist_visual_concepts"))
    calls.append(_call("score_visual_coherence"))
    calls.append(_call("persist_coherence_report"))
    return calls


def _escalate() -> list[dict[str, Any]]:
    return [_call("delegate_to_escalation", reason="visual_loop_cap")]


def _case(
    trajectory: list[dict[str, Any]], **metadata: Any
) -> EvaluationData[Any, Any]:
    return EvaluationData[Any, Any](
        input=None,
        actual_trajectory=trajectory,
        metadata=metadata,
    )


def _outputs_to_map(outputs: list[Any]) -> dict[str, Any]:
    return {o.label: o for o in outputs}


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_single_iteration_happy_path_passes_all_gates() -> None:
    scenes = 3
    phrases = 6
    trajectory = _bootstrap(scenes) + _score_block(phrases)
    case = _case(
        trajectory,
        expected_iterations=1,
        expected_scene_count=scenes,
        expects_pass=True,
    )
    results = _outputs_to_map(VisualLoopTrajectoryEvaluator().evaluate(case))
    for label in (
        "visual_loop.iteration_count",
        "visual_loop.shape",
        "visual_loop.bootstrap_once",
        "visual_loop.forbidden_launch",
        "visual_loop.delegation",
    ):
        assert label in results, f"missing evaluator output {label}"
        assert results[label].test_pass, f"{label} failed: {results[label].reason}"


def test_two_iteration_revision_pinpoints_weak_scenes() -> None:
    scenes = 3
    phrases = 6
    revision_count = 2  # revise 2 out of 6 phrases
    trajectory = (
        _bootstrap(scenes) + _score_block(phrases) + _score_block(revision_count)
    )
    case = _case(
        trajectory,
        expected_iterations=2,
        expected_scene_count=scenes,
        expected_revision_counts=[revision_count],
        expects_pass=True,
    )
    results = _outputs_to_map(VisualLoopTrajectoryEvaluator().evaluate(case))
    for label in (
        "visual_loop.iteration_count",
        "visual_loop.shape",
        "visual_loop.bootstrap_once",
        "visual_loop.revision_scope",
        "visual_loop.forbidden_launch",
        "visual_loop.delegation",
    ):
        assert label in results, f"missing evaluator output {label}"
        assert results[label].test_pass, f"{label} failed: {results[label].reason}"


# ---------------------------------------------------------------------------
# Regression: bootstrap must appear only on iteration 1
# ---------------------------------------------------------------------------


def test_rerunning_bootstrap_on_iteration_two_fails_gate() -> None:
    scenes = 3
    phrases = 6
    # Iteration 2 re-runs extract_phrases — regression.
    trajectory = (
        _bootstrap(scenes)
        + _score_block(phrases)
        + [_call("extract_phrases", scene_num=1)]
        + _score_block(2)
    )
    case = _case(
        trajectory,
        expected_iterations=2,
        expected_scene_count=scenes,
        expected_revision_counts=[2],
        expects_pass=True,
    )
    results = _outputs_to_map(VisualLoopTrajectoryEvaluator().evaluate(case))
    assert not results["visual_loop.bootstrap_once"].test_pass, (
        "bootstrap_once gate passed even though iteration 2 re-ran extract_phrases"
    )


# ---------------------------------------------------------------------------
# Cap behavior: hitting the cap must delegate to escalation
# ---------------------------------------------------------------------------


def test_iteration_cap_requires_escalation_delegation() -> None:
    scenes = 3
    phrases = 6
    iters = VISUAL_LOOP_MAX_ITERATIONS  # 5
    # Iteration 1 is the bootstrap+score; iterations 2..5 are revisions
    # of 2 weak phrases each.
    trajectory = _bootstrap(scenes) + _score_block(phrases)
    for _ in range(iters - 1):
        trajectory += _score_block(2)
    trajectory += _escalate()

    case = _case(
        trajectory,
        expected_iterations=iters,
        expected_scene_count=scenes,
        expected_revision_counts=[2] * (iters - 1),
        expects_pass=False,
        expects_delegation=True,
    )
    results = _outputs_to_map(VisualLoopTrajectoryEvaluator().evaluate(case))
    for label in (
        "visual_loop.iteration_count",
        "visual_loop.shape",
        "visual_loop.bootstrap_once",
        "visual_loop.revision_scope",
        "visual_loop.forbidden_launch",
        "visual_loop.delegation",
    ):
        assert label in results, f"missing evaluator output {label}"
        assert results[label].test_pass, f"{label} failed: {results[label].reason}"


def test_iteration_cap_without_delegation_fails_gate() -> None:
    """Loop maxes out at 5 iterations but never delegates — must fail."""
    scenes = 3
    phrases = 6
    iters = VISUAL_LOOP_MAX_ITERATIONS
    trajectory = _bootstrap(scenes) + _score_block(phrases)
    for _ in range(iters - 1):
        trajectory += _score_block(2)
    # NO delegation.

    case = _case(
        trajectory,
        expected_iterations=iters,
        expected_scene_count=scenes,
        expected_revision_counts=[2] * (iters - 1),
        expects_pass=False,
        expects_delegation=True,
    )
    results = _outputs_to_map(VisualLoopTrajectoryEvaluator().evaluate(case))
    assert not results["visual_loop.delegation"].test_pass, (
        "delegation gate passed even though escalation was skipped at cap"
    )


# ---------------------------------------------------------------------------
# Regression: launch_* tools are forbidden inside the visual loop
# ---------------------------------------------------------------------------


def test_launch_tool_inside_loop_fails_gate() -> None:
    scenes = 3
    phrases = 6
    # Someone tried to render clips inside the visual loop — forbidden.
    trajectory = (
        _bootstrap(scenes)
        + _score_block(phrases)
        + [_call("launch_visual_production", phrase_id="ph-a")]
    )
    case = _case(
        trajectory,
        expected_iterations=1,
        expected_scene_count=scenes,
        expects_pass=True,
    )
    results = _outputs_to_map(VisualLoopTrajectoryEvaluator().evaluate(case))
    assert not results["visual_loop.forbidden_launch"].test_pass, (
        "forbidden_launch gate passed even though launch_visual_production "
        "appeared inside the visual loop"
    )
