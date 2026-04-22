"""Trajectory tests for production-to-assembly ordering invariants.

Drives the real DeepAgent orchestrator through scripted production +
assembly scenarios, extracts the tool-call trajectory, and grades it
with :class:`AssemblyOrderingEvaluator`. Each scenario pins one branch
of AGENTS.md hard invariants #2, #5, and #6 as they compose at the
orchestrator layer:

* **Happy path** — ``check_worker_health`` → per-scene
  ``launch_visual_production`` → per-scene
  ``evaluate_visual_artifact_quality`` → ``assemble_final_cut``. All
  three gates pass.
* **Missing health check** — launches fire without any prior
  ``check_worker_health``. The ``health_check_first`` gate fails.
* **Late health check** — health is called *after* the first launch.
  Same gate fails with a "fires after" reason.
* **Missing QA for a scene** — one scene is launched but never
  QA'd / skipped / escalated. The ``qa_after_each_launch`` gate fails
  and names the scene.
* **Skip-scene terminates cleanly** — a launched scene is marked
  ``skip_scene`` instead of QA'd; assembly still happens. Gates pass.
* **Assembly before QA** — ``assemble_final_cut`` fires while a scene
  is still pending QA. The ``no_pending_at_assembly`` gate fails.

The stub tools keep signatures minimal — they mirror only the
arguments the evaluator reads (``scene_id``) and return a short
string so the scripted LLM turn can advance. No GPU, no B2, no real
QA.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool

from strands_agents.evals.evaluators.assembly_ordering import (
    AssemblyOrderingEvaluator,
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
# Stub tools. Argument signatures match only what the evaluator inspects.
# ---------------------------------------------------------------------------


@tool
def check_worker_health() -> str:
    """Stub for the real GPU worker-health probe."""
    return "ready"


@tool
def launch_visual_production(scene_id: str) -> str:
    """Stub for the real per-scene LTX dispatch."""
    return f"launched:{scene_id}"


@tool
def evaluate_visual_artifact_quality(scene_id: str) -> str:
    """Stub for the deterministic per-clip QA."""
    return f"qa_pass:{scene_id}"


@tool
def skip_scene(scene_id: str, reason: str = "") -> str:
    """Stub for the recovery skip action."""
    return f"skipped:{scene_id}"


@tool
def request_escalation(scene_id: str, reason: str = "") -> str:
    """Stub for the escalation request."""
    return f"escalated:{scene_id}"


@tool
def await_tasks(task_ids: list[str] | None = None) -> str:
    """Stub for the task-pool awaiter."""
    return "ok"


@tool
def assemble_final_cut() -> str:
    """Stub for the final-cut assembly tool."""
    return "assembled"


_ALL_STUB_TOOLS = [
    check_worker_health,
    launch_visual_production,
    evaluate_visual_artifact_quality,
    skip_scene,
    request_escalation,
    await_tasks,
    assemble_final_cut,
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
    # Disable approval interrupts so launch_visual_production runs to
    # completion under the scripted trajectory. The gates being tested
    # here are ordering gates, not human-in-the-loop gates — PR-S7 will
    # cover approval-reject scenarios separately.
    sim.with_interrupt_tool_names(())
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
    """Return the evaluator output for ``label``, failing the test on miss."""
    for output in outputs:
        if output.label == label:
            return output
    labels = [o.label for o in outputs]
    pytest.fail(f"missing evaluator gate {label!r}; got {labels!r}")


def _grade(
    result: SimulationResult, **metadata: Any
) -> list[EvaluationOutput]:
    trajectory = tool_call_trajectory(result)
    return AssemblyOrderingEvaluator().evaluate(
        EvaluationData(
            input="brief",
            actual_trajectory=trajectory,
            metadata=metadata or None,
        )
    )


def _launch(scene_id: str) -> tuple[str, dict[str, Any]]:
    return "launch_visual_production", {"scene_id": scene_id}


def _qa(scene_id: str) -> tuple[str, dict[str, Any]]:
    return "evaluate_visual_artifact_quality", {"scene_id": scene_id}


def _skip(scene_id: str) -> tuple[str, dict[str, Any]]:
    return "skip_scene", {"scene_id": scene_id, "reason": "test"}


def _health() -> Any:
    return scripted_tool_call("check_worker_health", {})


def _assembly() -> Any:
    return scripted_tool_call("assemble_final_cut", {})


def _await() -> Any:
    return scripted_tool_call("await_tasks", {"task_ids": []})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Health → launches → QAs → assembly. Every gate passes."""

    def test_two_scenes_clean_path_passes_every_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                _health(),
                scripted_parallel_tool_calls(_launch("s1"), _launch("s2")),
                _await(),
                scripted_parallel_tool_calls(_qa("s1"), _qa("s2")),
                _assembly(),
                scripted_final("documentary assembled"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        assert _gate(outputs, "assembly.health_check_first").test_pass
        assert _gate(outputs, "assembly.qa_after_each_launch").test_pass
        assert _gate(outputs, "assembly.no_pending_at_assembly").test_pass


# ---------------------------------------------------------------------------
# Health-check first gate
# ---------------------------------------------------------------------------


class TestHealthCheckFirst:
    """Gate: check_worker_health must precede any launch."""

    def test_missing_health_fails_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                scripted_parallel_tool_calls(_launch("s1")),
                _await(),
                scripted_tool_call(*_qa("s1")),
                _assembly(),
                scripted_final("assembled without health"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(outputs, "assembly.health_check_first")
        assert gate.test_pass is False
        assert "without any prior check_worker_health" in gate.reason

    def test_late_health_fails_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                scripted_parallel_tool_calls(_launch("s1")),
                _await(),
                _health(),  # fires AFTER the first launch — invalid
                scripted_tool_call(*_qa("s1")),
                _assembly(),
                scripted_final("late health"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(outputs, "assembly.health_check_first")
        assert gate.test_pass is False
        assert "fires after" in gate.reason


# ---------------------------------------------------------------------------
# QA-after-each-launch gate
# ---------------------------------------------------------------------------


class TestQaAfterLaunch:
    """Gate: every launched scene must be QA'd (or skipped/escalated)."""

    def test_missing_qa_for_one_scene_fails_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                _health(),
                scripted_parallel_tool_calls(_launch("s1"), _launch("s2")),
                _await(),
                scripted_tool_call(*_qa("s1")),  # s2 never QA'd
                _assembly(),
                scripted_final("partial QA"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(outputs, "assembly.qa_after_each_launch")
        assert gate.test_pass is False
        assert "s2" in gate.reason

    def test_skip_scene_satisfies_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                _health(),
                scripted_parallel_tool_calls(_launch("s1"), _launch("s2")),
                _await(),
                scripted_parallel_tool_calls(_qa("s1"), _skip("s2")),
                _assembly(),
                scripted_final("one skipped, one rendered"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        assert _gate(outputs, "assembly.qa_after_each_launch").test_pass
        assert _gate(outputs, "assembly.no_pending_at_assembly").test_pass


# ---------------------------------------------------------------------------
# No-pending-at-assembly gate
# ---------------------------------------------------------------------------


class TestNoPendingAtAssembly:
    """Gate: assemble_final_cut must come after every scene is terminal."""

    def test_assembly_before_qa_fails_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        # Assembly fires while s2 is still waiting for QA. Evaluator
        # must catch this even though the QA eventually appears later.
        simulator.with_chat_responses(
            [
                _health(),
                scripted_parallel_tool_calls(_launch("s1"), _launch("s2")),
                _await(),
                scripted_tool_call(*_qa("s1")),
                _assembly(),  # fires while s2 has no terminal signal yet
                scripted_tool_call(*_qa("s2")),
                scripted_final("post-assembly QA"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(outputs, "assembly.no_pending_at_assembly")
        assert gate.test_pass is False
        assert "s2" in gate.reason
