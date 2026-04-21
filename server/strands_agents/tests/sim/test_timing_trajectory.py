"""Trajectory tests for the timing-loop hard invariants.

The timing loop is described in
``docs/strands-migration/components/05-timing-loop.md`` and by
:class:`~strands_agents.evals.evaluators.timing_loop_trajectory.TimingLoopTrajectoryEvaluator`.
The invariants under test here come straight from that evaluator:

1. Each iteration is ``launch_audio_render+ → await_tasks → evaluate_timing``
   (batched launches, exactly one await, exactly one evaluation).
2. A failing iteration may be followed by at most one ``refine_scenario``
   (optionally paired with ``write_file``).
3. Every ``refine_scenario`` must carry a non-empty ``timing_report``.
4. Iteration count is hard-capped at 10. When the cap is reached the
   orchestrator must delegate to the ``escalation`` SubAgent
   (``task(subagent_type="escalation", ...)`` or a
   ``delegate_to_escalation`` tool call).

Strategy
--------

These tests do not invoke the real timing-loop tools — they don't
need TTS, GPU, or an alignment model. Instead, the simulator registers
lightweight stub ``@tool`` implementations for each tool the loop
calls (``launch_audio_render``, ``await_tasks``, ``evaluate_timing``,
``refine_scenario``, ``write_file``, ``task``).  The scripted LLM is
then driven to emit tool calls in the exact shape each test wants
to prove (happy-path, multi-iteration, cap-hit-with-delegation,
refiner-without-report, broken-shape).

The trajectory the orchestrator actually emits is then extracted via
:func:`tool_call_trajectory` and fed into the *real*
:class:`TimingLoopTrajectoryEvaluator`.  The test asserts on the
evaluator's gate outputs.  This proves three things in one shot:

* The simulator faithfully drives the real orchestrator through a
  scripted trajectory.
* The extractor correctly reads the emitted tool-call sequence off
  the final-state messages.
* The evaluator's gates fire when — and only when — the invariants
  are honoured.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool

from strands_agents.evals.evaluators.timing_loop_trajectory import (
    TimingLoopTrajectoryEvaluator,
)
from strands_agents.sim import (
    OrchestratorSimulator,
    SimulationResult,
    scripted_final,
    scripted_parallel_tool_calls,
    scripted_tool_call,
    tool_call_trajectory,
)
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput


# ---------------------------------------------------------------------------
# Stub tools shared across every test in this module.
#
# They mimic the real timing-loop tools' call signatures closely enough
# for the simulator's ToolNode to dispatch them. None of them do real
# work — they just return a short string so the LLM turn can continue.
# ---------------------------------------------------------------------------


@tool
def launch_audio_render(scene_id: str, voice: str, text: str, revision: str) -> str:
    """Stub for the real ``launch_audio_render`` tool."""
    return f"launched:{scene_id}"


@tool
def await_tasks(task_ids: list[str] | None = None) -> str:
    """Stub for the real ``await_tasks`` tool."""
    return "ok"


@tool
def evaluate_timing(scenes: Any = None, alignments: Any = None) -> str:
    """Stub for the real ``evaluate_timing`` tool."""
    return "scored"


@tool
def refine_scenario(
    scenes: Any = None,
    timing_report: Any = None,
    revision: str = "",
) -> str:
    """Stub for the real ``refine_scenario`` tool."""
    return "refined"


@tool
def write_file(path: str, content: str) -> str:
    """Stub for the filesystem ``write_file`` tool."""
    return "wrote"


@tool
def task(subagent_type: str, description: str) -> str:
    """Stub for the DeepAgent ``task`` delegation tool."""
    return f"delegated:{subagent_type}"


_ALL_STUB_TOOLS = [
    launch_audio_render,
    await_tasks,
    evaluate_timing,
    refine_scenario,
    write_file,
    task,
]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def simulator() -> Iterator[OrchestratorSimulator]:
    sim = OrchestratorSimulator()
    sim.with_tools(_ALL_STUB_TOOLS)
    try:
        yield sim
    finally:
        sim.shutdown()


def _run(sim: OrchestratorSimulator, brief: str, run_dir: Path) -> SimulationResult:
    return asyncio.run(sim.run(brief, run_dir))


def _gate(
    outputs: list[EvaluationOutput],
    label: str,
) -> EvaluationOutput:
    """Find an evaluator output by label. Fails the test on missing labels."""
    for output in outputs:
        if output.label == label:
            return output
    labels = [o.label for o in outputs]
    pytest.fail(f"missing evaluator gate {label!r}; got {labels!r}")


def _timing_report(
    *,
    worst: float = 0.6,
    scenes: int = 3,
) -> dict[str, Any]:
    """Minimal non-empty ``timing_report`` payload for ``refine_scenario``."""
    return {
        "worst_deviation": worst,
        "timing_passed": False,
        "per_scene": [
            {"scene_id": f"s{i}", "deviation": worst - 0.1} for i in range(scenes)
        ],
    }


def _iteration_turns(
    iteration_index: int,
    *,
    scene_count: int = 3,
    include_refine: bool = True,
    timing_report: dict[str, Any] | None = None,
) -> list[Any]:
    """Build the LLM turns for one timing-loop iteration.

    * Turn 1: parallel ``launch_audio_render`` calls, one per scene.
    * Turn 2: single ``await_tasks`` call.
    * Turn 3: single ``evaluate_timing`` call.
    * Turn 4 (optional): ``refine_scenario`` carrying ``timing_report``.

    The ``iteration_index`` is threaded into scene ids / revision
    strings so successive iterations produce different tool-call
    args — this matches what the real orchestrator would emit.
    """
    launch_calls = [
        (
            "launch_audio_render",
            {
                "scene_id": f"s{scene}_iter{iteration_index}",
                "voice": "narrator",
                "text": f"scene {scene} copy",
                "revision": f"rev-{iteration_index}",
            },
        )
        for scene in range(scene_count)
    ]

    turns: list[Any] = [
        scripted_parallel_tool_calls(*launch_calls),
        scripted_tool_call("await_tasks", {"task_ids": []}),
        scripted_tool_call("evaluate_timing", {}),
    ]

    if include_refine:
        report = timing_report if timing_report is not None else _timing_report()
        turns.append(
            scripted_tool_call(
                "refine_scenario",
                {"timing_report": report, "revision": f"rev-{iteration_index + 1}"},
            )
        )

    return turns


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTimingLoopHappyPath:
    """Single iteration, timing passes on first try, no refine needed."""

    def test_one_iteration_passes_every_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                *_iteration_turns(0, include_refine=False),
                scripted_final("timing loop converged"),
            ]
        )
        result = _run(simulator, "brief", run_dir)
        trajectory = tool_call_trajectory(result)

        evaluator = TimingLoopTrajectoryEvaluator()
        outputs = evaluator.evaluate(
            EvaluationData(
                input="brief",
                actual_trajectory=trajectory,
                metadata={"expected_iterations": 1, "expects_pass": True},
            )
        )

        assert _gate(outputs, "timing_loop.iteration_count").test_pass
        assert _gate(outputs, "timing_loop.shape").test_pass
        assert _gate(outputs, "timing_loop.refine_count").test_pass
        # No refines on the happy path, so the refine_inputs gate is
        # only emitted when there *were* refine calls. Confirm it's
        # absent to match that branch of the evaluator.
        assert not any(o.label == "timing_loop.refine_inputs" for o in outputs)
        assert _gate(outputs, "timing_loop.delegation").test_pass


class TestTimingLoopMultiIterationConvergence:
    """Three iterations: fail, fail, pass. Two refines in between."""

    def test_three_iterations_all_gates_pass(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                *_iteration_turns(0, include_refine=True),
                *_iteration_turns(1, include_refine=True),
                *_iteration_turns(2, include_refine=False),
                scripted_final("converged on iteration 3"),
            ]
        )
        result = _run(simulator, "brief", run_dir)
        trajectory = tool_call_trajectory(result)

        evaluator = TimingLoopTrajectoryEvaluator()
        outputs = evaluator.evaluate(
            EvaluationData(
                input="brief",
                actual_trajectory=trajectory,
                metadata={"expected_iterations": 3, "expects_pass": True},
            )
        )

        for label in (
            "timing_loop.iteration_count",
            "timing_loop.shape",
            "timing_loop.refine_count",
            "timing_loop.refine_inputs",
            "timing_loop.delegation",
        ):
            gate = _gate(outputs, label)
            assert gate.test_pass, f"{label}: {gate.reason}"


class TestTimingLoopTenIterationCap:
    """Ten failing iterations must end in delegation to escalation."""

    def test_cap_hit_with_escalation_delegation_passes(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        iteration_turns: list[Any] = []
        # Ten iterations: nine refine, the tenth does not (the
        # orchestrator delegates instead of refining further).
        for i in range(10):
            include_refine = i < 9
            iteration_turns.extend(
                _iteration_turns(i, include_refine=include_refine)
            )
        iteration_turns.extend(
            [
                scripted_tool_call(
                    "task",
                    {
                        "subagent_type": "escalation",
                        "description": "timing loop hit 10-iteration cap",
                    },
                ),
                scripted_final("escalated"),
            ]
        )
        simulator.with_chat_responses(iteration_turns)
        result = _run(simulator, "brief", run_dir)
        trajectory = tool_call_trajectory(result)

        evaluator = TimingLoopTrajectoryEvaluator()
        outputs = evaluator.evaluate(
            EvaluationData(
                input="brief",
                actual_trajectory=trajectory,
                metadata={
                    "expected_iterations": 10,
                    "expects_pass": False,
                    "expects_delegation": True,
                    # Nine refines: one per failed iteration except the
                    # last, which escalates instead of refining.
                    "expected_refines": 9,
                },
            )
        )

        for label in (
            "timing_loop.iteration_count",
            "timing_loop.shape",
            "timing_loop.refine_count",
            "timing_loop.refine_inputs",
            "timing_loop.delegation",
        ):
            gate = _gate(outputs, label)
            assert gate.test_pass, f"{label}: {gate.reason}"


class TestTimingLoopInvariantViolations:
    """The evaluator must *fail* when the orchestrator violates an
    invariant.  These tests are the necessary dual of the happy-path
    tests — without them a permissive evaluator would silently pass
    everything.
    """

    def test_refine_without_timing_report_fails_refine_inputs_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        # Single failing iteration, then a refine call that omits
        # timing_report entirely.  The refine_inputs gate must fail.
        simulator.with_chat_responses(
            [
                *_iteration_turns(0, include_refine=False),
                scripted_tool_call("refine_scenario", {"revision": "rev-1"}),
                *_iteration_turns(1, include_refine=False),
                scripted_final("converged"),
            ]
        )
        result = _run(simulator, "brief", run_dir)
        trajectory = tool_call_trajectory(result)

        evaluator = TimingLoopTrajectoryEvaluator()
        outputs = evaluator.evaluate(
            EvaluationData(
                input="brief",
                actual_trajectory=trajectory,
                metadata={"expected_iterations": 2, "expects_pass": True},
            )
        )

        assert _gate(outputs, "timing_loop.iteration_count").test_pass
        assert _gate(outputs, "timing_loop.shape").test_pass
        assert _gate(outputs, "timing_loop.refine_count").test_pass
        # The one refine had no timing_report, so this gate fails.
        assert not _gate(outputs, "timing_loop.refine_inputs").test_pass

    def test_missing_await_tasks_fails_shape_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        # Emit launches and evaluate directly, skipping await_tasks.
        # The shape gate must flag the iteration as malformed.
        launch_calls = [
            (
                "launch_audio_render",
                {
                    "scene_id": f"s{scene}_iter0",
                    "voice": "narrator",
                    "text": f"copy {scene}",
                    "revision": "rev-0",
                },
            )
            for scene in range(3)
        ]
        simulator.with_chat_responses(
            [
                scripted_parallel_tool_calls(*launch_calls),
                scripted_tool_call("evaluate_timing", {}),
                scripted_final("bad shape"),
            ]
        )
        result = _run(simulator, "brief", run_dir)
        trajectory = tool_call_trajectory(result)

        evaluator = TimingLoopTrajectoryEvaluator()
        outputs = evaluator.evaluate(
            EvaluationData(
                input="brief",
                actual_trajectory=trajectory,
                metadata={"expected_iterations": 1, "expects_pass": True},
            )
        )

        # The evaluator must treat the missing await as a shape break.
        assert not _gate(outputs, "timing_loop.shape").test_pass

    def test_missing_delegation_at_cap_fails_delegation_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        # Ten failing iterations, nine refines, but no delegation at
        # the end.  The delegation gate must flag this.
        iteration_turns: list[Any] = []
        for i in range(10):
            iteration_turns.extend(
                _iteration_turns(i, include_refine=(i < 9))
            )
        iteration_turns.append(scripted_final("gave up silently"))
        simulator.with_chat_responses(iteration_turns)
        result = _run(simulator, "brief", run_dir)
        trajectory = tool_call_trajectory(result)

        evaluator = TimingLoopTrajectoryEvaluator()
        outputs = evaluator.evaluate(
            EvaluationData(
                input="brief",
                actual_trajectory=trajectory,
                metadata={
                    "expected_iterations": 10,
                    "expects_pass": False,
                    "expects_delegation": True,
                    "expected_refines": 9,
                },
            )
        )

        assert not _gate(outputs, "timing_loop.delegation").test_pass
