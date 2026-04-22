"""Timing-loop experiment factory for component 05.

The timing loop is **not** a module — it is a trajectory the DeepAgent
orchestrator follows, using leaf tools from components 02 (``evaluate_timing``),
03 (``refine_scenario``) and 04 (``launch_audio_render`` / ``await_tasks``).
This experiment pins the expected trajectory and scores the orchestrator
against five canonical cases.

Cases:

1. ``one_shot_pass`` — 3 scenes, first render within ±2 s → 1 iteration,
   no refiner call.
2. ``one_refine_pass`` — 5 scenes, first render over by 6 s, refined
   render within tolerance → 2 iterations, 1 refine.
3. ``per_scene_spike`` — 5 scenes, total within tolerance but scene 3
   over by 20 % → 2 iterations, 1 refine targeted at scene 3.
4. ``refiner_no_op`` — 3 scenes, refiner returns byte-identical scenes
   across successive revisions. Per the AGENTS.md rule, two consecutive
   no-op refines short-circuit the loop → delegation to the escalation
   SubAgent on iteration 3, well under the 10-iteration cap.
5. ``max_iterations`` — 5 scenes, every iteration still off-target →
   hits the cap → delegation to escalation.

Evaluator stack mirrors ``eval-framework/THRESHOLDS.md``:

- :class:`TimingLoopTrajectoryEvaluator` (hard gate ≥0.90).
- :class:`ParallelLaunchEvaluator` (soft gate ≥0.80) — per-scene
  ``launch_audio_render`` calls are emitted in a single tool-call batch.
- :class:`ContractComplianceEvaluator` (hard gate 1.00) against
  :data:`TIMING_CONTRACT`.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment

from contracts import TIMING_CONTRACT
from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
    ParallelLaunchEvaluator,
    TimingLoopTrajectoryEvaluator,
)

#: Minimum per-evaluator score / hard-gate flags. Mirrors the timing
#: stage thresholds in ``eval-framework/THRESHOLDS.md``.
TIMING_LOOP_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "TimingLoopTrajectoryEvaluator": (0.90, True),
    "ParallelLaunchEvaluator": (0.80, False),
    "ContractComplianceEvaluator": (1.00, True),
}


def _launch_audio_call(
    scene_id: str,
    turn: int,
    *,
    revision: int = 1,
    language: str = "en",
    voice_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a synthetic ``launch_audio_render`` tool-call record.

    ``at_turn`` carries the batching marker the
    :class:`ParallelLaunchEvaluator` keys off — all launches within one
    iteration share the same turn number.
    """
    return {
        "name": "launch_audio_render",
        "at_turn": turn,
        "args": {
            "scene_id": scene_id,
            "revision": revision,
            "language": language,
            "voice_map": voice_map or {},
        },
    }


def _await_tasks_call(task_ids: list[str], turn: int) -> dict[str, Any]:
    return {
        "name": "await_tasks",
        "at_turn": turn,
        "args": {"task_ids": task_ids},
    }


def _evaluate_timing_call(turn: int, *, intent_sec: float) -> dict[str, Any]:
    return {
        "name": "evaluate_timing",
        "at_turn": turn,
        "args": {"intent_target_sec": intent_sec},
    }


def _refine_scenario_call(
    turn: int,
    *,
    timing_report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "name": "refine_scenario",
        "at_turn": turn,
        "args": {"timing_report": timing_report},
    }


def _write_file_call(turn: int, path: str) -> dict[str, Any]:
    return {
        "name": "write_file",
        "at_turn": turn,
        "args": {"path": path},
    }


def _delegate_escalation_call(turn: int, *, reason: str) -> dict[str, Any]:
    return {
        "name": "task",
        "at_turn": turn,
        "args": {
            "subagent_type": "escalation",
            "description": reason,
        },
    }


def _scenes(n: int) -> list[dict[str, Any]]:
    """Placeholder scene payload used by the contract-compliance state."""
    return [{"id": i + 1, "target_duration_sec": 18.0} for i in range(n)]


def _state_after_timing(
    *,
    scenes: list[dict[str, Any]],
    timing_passed: bool,
    iterations: int,
) -> dict[str, Any]:
    """Build the state dict consumed by :class:`ContractComplianceEvaluator`.

    Populates every key :data:`TIMING_CONTRACT` lists under
    ``required_state`` + ``produced_state`` with non-placeholder values.
    Artifacts (none for the timing contract) are ignored by the
    evaluator when the corresponding glob list is empty.
    """
    return {
        "scenes": scenes,
        "whisperx_alignment": {"total_duration_sec": 60.0, "per_clip": {}},
        "timing_passed": timing_passed,
        "timing_report": {
            "status": "PASS" if timing_passed else "FAIL",
            "iterations": iterations,
            "per_scene": [],
        },
    }


def _one_shot_pass() -> Case:
    scenes = _scenes(3)
    trajectory = [
        _launch_audio_call("s1", turn=1),
        _launch_audio_call("s2", turn=1),
        _launch_audio_call("s3", turn=1),
        _await_tasks_call(["t1", "t2", "t3"], turn=2),
        _evaluate_timing_call(turn=3, intent_sec=54.0),
    ]
    return Case(
        name="one_shot_pass",
        input={"scenes": scenes, "intent_target_sec": 54.0},
        expected_output={"timing_passed": True},
        expected_trajectory=trajectory,
        metadata={
            "case_name": "one_shot_pass",
            "final_state": _state_after_timing(
                scenes=scenes, timing_passed=True, iterations=1
            ),
            "expected_iterations": 1,
            "expected_refines": 0,
            "expects_pass": True,
            "expects_delegation": False,
            # ParallelLaunch
            "tool_name": "launch_audio_render",
            "expected_count": 3,
            "completion_tool": "await_tasks",
            # ContractCompliance
            "contract_name": "timing",
        },
    )


def _one_refine_pass() -> Case:
    scenes = _scenes(5)
    timing_report = {
        "status": "FAIL",
        "per_scene": [{"scene_num": 3, "delta_sec": 6.0}],
    }
    trajectory = [
        _launch_audio_call("s1", turn=1),
        _launch_audio_call("s2", turn=1),
        _launch_audio_call("s3", turn=1),
        _launch_audio_call("s4", turn=1),
        _launch_audio_call("s5", turn=1),
        _await_tasks_call(["t1", "t2", "t3", "t4", "t5"], turn=2),
        _evaluate_timing_call(turn=3, intent_sec=90.0),
        _refine_scenario_call(turn=4, timing_report=timing_report),
        _write_file_call(turn=4, path="scenes.json"),
        _launch_audio_call("s1", turn=5, revision=2),
        _launch_audio_call("s2", turn=5, revision=2),
        _launch_audio_call("s3", turn=5, revision=2),
        _launch_audio_call("s4", turn=5, revision=2),
        _launch_audio_call("s5", turn=5, revision=2),
        _await_tasks_call(["t6", "t7", "t8", "t9", "t10"], turn=6),
        _evaluate_timing_call(turn=7, intent_sec=90.0),
    ]
    return Case(
        name="one_refine_pass",
        input={"scenes": scenes, "intent_target_sec": 90.0},
        expected_output={"timing_passed": True},
        expected_trajectory=trajectory,
        metadata={
            "case_name": "one_refine_pass",
            "final_state": _state_after_timing(
                scenes=scenes, timing_passed=True, iterations=2
            ),
            "expected_iterations": 2,
            "expected_refines": 1,
            "expects_pass": True,
            "expects_delegation": False,
            "tool_name": "launch_audio_render",
            "expected_count": 5,
            "completion_tool": "await_tasks",
            "contract_name": "timing",
        },
    )


def _per_scene_spike() -> Case:
    scenes = _scenes(5)
    timing_report = {
        "status": "FAIL",
        "per_scene": [
            {"scene_num": 3, "delta_sec": 3.6, "relative": 0.20}
        ],
    }
    trajectory = [
        _launch_audio_call("s1", turn=1),
        _launch_audio_call("s2", turn=1),
        _launch_audio_call("s3", turn=1),
        _launch_audio_call("s4", turn=1),
        _launch_audio_call("s5", turn=1),
        _await_tasks_call(["t1", "t2", "t3", "t4", "t5"], turn=2),
        _evaluate_timing_call(turn=3, intent_sec=90.0),
        _refine_scenario_call(turn=4, timing_report=timing_report),
        _write_file_call(turn=4, path="scenes.json"),
        _launch_audio_call("s1", turn=5, revision=2),
        _launch_audio_call("s2", turn=5, revision=2),
        _launch_audio_call("s3", turn=5, revision=2),
        _launch_audio_call("s4", turn=5, revision=2),
        _launch_audio_call("s5", turn=5, revision=2),
        _await_tasks_call(["t6", "t7", "t8", "t9", "t10"], turn=6),
        _evaluate_timing_call(turn=7, intent_sec=90.0),
    ]
    return Case(
        name="per_scene_spike",
        input={"scenes": scenes, "intent_target_sec": 90.0},
        expected_output={"timing_passed": True},
        expected_trajectory=trajectory,
        metadata={
            "case_name": "per_scene_spike",
            "final_state": _state_after_timing(
                scenes=scenes, timing_passed=True, iterations=2
            ),
            "expected_iterations": 2,
            "expected_refines": 1,
            "expects_pass": True,
            "expects_delegation": False,
            "tool_name": "launch_audio_render",
            "expected_count": 5,
            "completion_tool": "await_tasks",
            "contract_name": "timing",
        },
    )


def _refiner_no_op() -> Case:
    # AGENTS.md rule: if two consecutive ``refine_scenario`` outputs are
    # byte-identical to the previous revision, short-circuit the loop and
    # delegate to escalation. The minimum trajectory that exercises this
    # is three iterations — iteration 1 produces the first refined
    # revision, iterations 2 and 3 produce two consecutive byte-identical
    # refines, and the orchestrator then delegates.
    scenes = _scenes(3)
    timing_report = {
        "status": "FAIL",
        "per_scene": [{"scene_num": 1, "delta_sec": 8.0}],
    }
    trajectory: list[dict[str, Any]] = []
    turn = 1
    for iteration in range(3):
        for scene_id in ("s1", "s2", "s3"):
            trajectory.append(
                _launch_audio_call(scene_id, turn=turn, revision=iteration + 1)
            )
        turn += 1
        trajectory.append(
            _await_tasks_call(
                [f"t{iteration}_{scene_id}" for scene_id in ("s1", "s2", "s3")],
                turn=turn,
            )
        )
        turn += 1
        trajectory.append(_evaluate_timing_call(turn=turn, intent_sec=54.0))
        turn += 1
        # Refine every iteration and persist back to the workspace per
        # the AGENTS.md rule. The refiner returns byte-identical scenes
        # in iterations 2 and 3, which the orchestrator must detect.
        trajectory.append(
            _refine_scenario_call(turn=turn, timing_report=timing_report)
        )
        trajectory.append(_write_file_call(turn=turn, path="scenes.json"))
        turn += 1
    trajectory.append(
        _delegate_escalation_call(
            turn=turn,
            reason=(
                "two consecutive refine_scenario outputs are byte-identical "
                "to the previous revision; refiner cannot converge the loop"
            ),
        )
    )
    return Case(
        name="refiner_no_op",
        input={"scenes": scenes, "intent_target_sec": 54.0},
        expected_output={"timing_passed": False, "escalated": True},
        expected_trajectory=trajectory,
        metadata={
            "case_name": "refiner_no_op",
            "final_state": _state_after_timing(
                scenes=scenes, timing_passed=False, iterations=3
            ),
            "expected_iterations": 3,
            "expected_refines": 3,
            "expects_pass": False,
            "expects_delegation": True,
            # ParallelLaunch — every iteration must dispatch 3 scenes in
            # one batch. Evaluator semantics are per-batch.
            "tool_name": "launch_audio_render",
            "expected_count": 3,
            "completion_tool": "await_tasks",
            "contract_name": "timing",
        },
    )


def _max_iterations() -> Case:
    scenes = _scenes(5)
    timing_report = {
        "status": "FAIL",
        "per_scene": [
            {"scene_num": i + 1, "delta_sec": 5.0 - i} for i in range(5)
        ],
    }
    trajectory: list[dict[str, Any]] = []
    turn = 1
    for iteration in range(10):
        for i in range(5):
            trajectory.append(
                _launch_audio_call(f"s{i + 1}", turn=turn, revision=iteration + 1)
            )
        turn += 1
        trajectory.append(
            _await_tasks_call(
                [f"t{iteration}_s{i + 1}" for i in range(5)],
                turn=turn,
            )
        )
        turn += 1
        trajectory.append(_evaluate_timing_call(turn=turn, intent_sec=90.0))
        turn += 1
        # Persist the refined scenes after every refine per AGENTS.md.
        trajectory.append(
            _refine_scenario_call(turn=turn, timing_report=timing_report)
        )
        trajectory.append(_write_file_call(turn=turn, path="scenes.json"))
        turn += 1
    trajectory.append(
        _delegate_escalation_call(
            turn=turn,
            reason="timing loop cap reached; could not converge",
        )
    )
    return Case(
        name="max_iterations",
        input={"scenes": scenes, "intent_target_sec": 90.0},
        expected_output={"timing_passed": False, "escalated": True},
        expected_trajectory=trajectory,
        metadata={
            "case_name": "max_iterations",
            "final_state": _state_after_timing(
                scenes=scenes, timing_passed=False, iterations=10
            ),
            "expected_iterations": 10,
            "expected_refines": 10,
            "expects_pass": False,
            "expects_delegation": True,
            "tool_name": "launch_audio_render",
            # Per-batch — every iteration dispatches 5 scenes at once.
            "expected_count": 5,
            "completion_tool": "await_tasks",
            "contract_name": "timing",
        },
    )


def timing_loop_cases() -> list[Case]:
    """Return the five canonical timing-loop trajectory cases."""
    return [
        _one_shot_pass(),
        _one_refine_pass(),
        _per_scene_spike(),
        _refiner_no_op(),
        _max_iterations(),
    ]


def timing_loop_evaluators() -> list[Evaluator]:
    """Return the timing-loop evaluator stack in spec order."""
    return [
        TimingLoopTrajectoryEvaluator(),
        ParallelLaunchEvaluator(),
        ContractComplianceEvaluator(TIMING_CONTRACT),
    ]


def build_timing_loop_experiment() -> Experiment:
    """Build the timing-loop experiment for the strands-evals runner."""
    return Experiment(
        cases=timing_loop_cases(),
        evaluators=timing_loop_evaluators(),
    )


def timing_loop_task(case: Case) -> dict[str, Any]:
    """Replay task adapter for the component-playground surface.

    Returns the case's canonical envelope so the evaluate endpoint can
    score a known-good payload against this component's evaluator
    stack without a live agent run. A live runner can replace this
    once provider plumbing lands in the playground.
    """
    metadata = case.metadata or {}
    expected_output: Any = (
        case.expected_output if case.expected_output is not None else {}
    )
    trajectory = case.expected_trajectory
    if trajectory is None:
        trajectory = metadata.get("canonical_trajectory")
    if trajectory is None:
        trajectory = []
    return {
        "output": expected_output,
        "trajectory": list(trajectory),
        "metadata": {"mode": "replay", "case": case.name},
    }


__all__ = [
    "TIMING_LOOP_EVALUATOR_THRESHOLDS",
    "build_timing_loop_experiment",
    "timing_loop_cases",
    "timing_loop_evaluators",
    "timing_loop_task",
]
