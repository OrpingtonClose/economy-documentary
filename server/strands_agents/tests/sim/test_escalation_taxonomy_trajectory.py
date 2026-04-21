"""Trajectory tests for the escalation taxonomy.

Drives the real DeepAgent orchestrator through scripted failure
scenarios, extracts the tool-call trajectory, and grades it with
:class:`EscalationTaxonomyEvaluator`. Each scenario pins one branch of
the escalation rules in AGENTS.md:

* **Two failures on the same scene** — the orchestrator must escalate
  before attempting a third run of the same scene/tool. Covers the
  "Retry policy" rule and AGENTS.md §"When to call the escalation
  SubAgent" trigger 1.
* **Refiner no-op** — when two consecutive ``refine_scenario`` outputs
  are byte-identical, the orchestrator must stop iterating and
  escalate before calling the refiner again. Covers AGENTS.md
  §"Timing stage" rule 4.
* **Approval reject** — when a human rejects an approval gate, the
  next attempt must not retry with identical arguments. Covers
  AGENTS.md hard invariant #9 ("a ``reject`` means re-plan, not
  retry-with-same-args").

The stub tools match only the arguments the evaluator reads (scene
ids, rejected args). Real TTS, GPU, and escalation SubAgents are not
in the loop — these tests grade the *orchestrator's sequencing
decisions*, not the correctness of the downstream work.

Each scenario uses three kinds of stub tool:

1. **Workers** (``launch_visual_production``, ``launch_audio_render``,
   ``refine_scenario``): the things the orchestrator calls to do
   work. Arguments are captured by the trajectory.
2. **Markers** (``record_scene_failure``, ``record_refiner_noop``,
   ``record_approval_reject``): the things the orchestrator (in a
   real run) would emit to signal that a failure/no-op/reject just
   happened. In production these come from tool return values, state
   patches, or interrupt handlers; in the simulator we script them
   explicitly so the evaluator can grade the sequence.
3. **Escalation handoffs** (``request_escalation``, ``task``): how
   the orchestrator delegates to the escalation SubAgent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool

from strands_agents.evals.evaluators.escalation_taxonomy import (
    EscalationTaxonomyEvaluator,
)
from strands_agents.sim import (
    OrchestratorSimulator,
    SimulationResult,
    scripted_final,
    scripted_tool_call,
    tool_call_trajectory,
)
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

# ---------------------------------------------------------------------------
# Stub tools. Argument signatures match only what the evaluator inspects.
# ---------------------------------------------------------------------------


@tool
def launch_visual_production(scene_id: str, seed: int = 0) -> str:
    """Stub for the real per-scene LTX dispatch."""
    return f"launched:{scene_id}:seed={seed}"


@tool
def launch_audio_render(scene_id: str, voice_id: str = "v1") -> str:
    """Stub for the real per-scene TTS dispatch."""
    return f"audio_launched:{scene_id}:{voice_id}"


@tool
def refine_scenario(revision: str = "") -> str:
    """Stub for the scenario refiner."""
    return f"refined:{revision}"


@tool
def record_scene_failure(scene_id: str, tool: str) -> str:
    """Marker tool: a previous ``scene_id`` render failed.

    In production the orchestrator would learn this from tool return
    values or state patches. In the simulator we script it explicitly
    so the evaluator can see exactly when each failure occurred.
    """
    return f"failure_recorded:{scene_id}:{tool}"


@tool
def record_refiner_noop() -> str:
    """Marker tool: two consecutive refiner outputs were byte-identical."""
    return "refiner_noop_recorded"


@tool
def record_approval_reject(tool: str, rejected_args_json: str) -> str:
    """Marker tool: a human rejected the pending approval gate.

    ``rejected_args_json`` is a JSON-encoded copy of the rejected
    call's arguments so the evaluator can check that any subsequent
    retry changes them. Encoding as a string avoids LangChain's
    structured-tool schema spreading ``dict[str, Any]`` fields as
    kwargs.
    """
    return f"reject_recorded:{tool}"


@tool
def request_escalation(scene_id: str = "", reason: str = "") -> str:
    """Stub for the explicit escalation request."""
    return f"escalated:{scene_id}"


@tool
def task(subagent_type: str, prompt: str = "") -> str:
    """Stub for delegation to a SubAgent (incl. ``escalation``)."""
    return f"delegated:{subagent_type}"


_ALL_STUB_TOOLS = [
    launch_visual_production,
    launch_audio_render,
    refine_scenario,
    record_scene_failure,
    record_refiner_noop,
    record_approval_reject,
    request_escalation,
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
    # Escalation scenarios grade the orchestrator's *sequencing*
    # decisions, not its human-in-the-loop handling. The approval-gate
    # itself is represented by the scripted ``record_approval_reject``
    # marker, not by a real interrupt round-trip — real interrupts
    # live in the simulator's approval-gate fixtures elsewhere.
    sim.with_interrupt_tool_names(())
    try:
        yield sim
    finally:
        sim.shutdown()


def _run(
    sim: OrchestratorSimulator, brief: str, run_dir: Path
) -> SimulationResult:
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
    return EscalationTaxonomyEvaluator().evaluate(
        EvaluationData(
            input="brief",
            actual_trajectory=trajectory,
            metadata=metadata or None,
        )
    )


# Convenience builders that emit single scripted tool calls.


def _launch(scene_id: str, seed: int = 0) -> Any:
    return scripted_tool_call(
        "launch_visual_production", {"scene_id": scene_id, "seed": seed}
    )


def _audio(scene_id: str, voice_id: str = "v1") -> Any:
    return scripted_tool_call(
        "launch_audio_render", {"scene_id": scene_id, "voice_id": voice_id}
    )


def _refine(revision: str = "r1") -> Any:
    return scripted_tool_call("refine_scenario", {"revision": revision})


def _fail(scene_id: str, tool: str = "launch_visual_production") -> Any:
    return scripted_tool_call(
        "record_scene_failure", {"scene_id": scene_id, "tool": tool}
    )


def _noop() -> Any:
    return scripted_tool_call("record_refiner_noop", {})


def _reject(tool: str, args: dict[str, Any]) -> Any:
    return scripted_tool_call(
        "record_approval_reject",
        {"tool": tool, "rejected_args_json": json.dumps(args, sort_keys=True)},
    )


def _escalate(scene_id: str = "", reason: str = "") -> Any:
    return scripted_tool_call(
        "request_escalation", {"scene_id": scene_id, "reason": reason}
    )


def _task_escalation() -> Any:
    return scripted_tool_call(
        "task", {"subagent_type": "escalation", "prompt": "handle failure"}
    )


# ---------------------------------------------------------------------------
# Two failures on the same scene
# ---------------------------------------------------------------------------


class TestTwoFailuresEscalate:
    """AGENTS.md: 'If the same scene fails twice, delegate to escalation.'"""

    def test_two_failures_then_escalate_passes_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                _launch("s1"),
                _fail("s1"),
                _launch("s1"),
                _fail("s1"),
                _escalate("s1", "two failures"),
                scripted_final("escalated s1"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(outputs, "escalation.two_failures_trigger")
        assert gate.test_pass is True

    def test_two_failures_third_retry_fails_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                _launch("s1"),
                _fail("s1"),
                _launch("s1"),
                _fail("s1"),
                _launch("s1"),  # violation — no escalation first
                scripted_final("bad retry"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(outputs, "escalation.two_failures_trigger")
        assert gate.test_pass is False
        assert "'s1'" in gate.reason

    def test_two_failures_audio_pool_escalate_via_task_passes(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        # Audio-render failures count too. Delegation via
        # ``task(subagent_type="escalation")`` (no scene_id) covers
        # every pending failure, matching the AGENTS.md hand-off.
        simulator.with_chat_responses(
            [
                _audio("s1"),
                _fail("s1", tool="launch_audio_render"),
                _audio("s1"),
                _fail("s1", tool="launch_audio_render"),
                _task_escalation(),
                scripted_final("escalated"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(outputs, "escalation.two_failures_trigger")
        assert gate.test_pass is True

    def test_two_failures_never_escalated_fails_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        # The orchestrator dropped the scene on the floor — two
        # failures recorded, no escalation, no further attempts. Still
        # a violation: the escalation SubAgent never got a chance to
        # see the diagnostic context.
        simulator.with_chat_responses(
            [
                _launch("s1"),
                _fail("s1"),
                _launch("s1"),
                _fail("s1"),
                scripted_final("silently dropped s1"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(outputs, "escalation.two_failures_trigger")
        assert gate.test_pass is False
        assert "never escalated" in gate.reason


# ---------------------------------------------------------------------------
# Refiner no-op
# ---------------------------------------------------------------------------


class TestRefinerNoopEscalate:
    """AGENTS.md: 'Two byte-identical refiner outputs → escalate.'"""

    def test_refiner_noop_then_escalate_passes_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                _refine("r1"),
                _refine("r1"),  # second call returns identical output
                _noop(),
                _task_escalation(),
                scripted_final("escalated refiner noop"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(outputs, "escalation.refiner_noop_trigger")
        assert gate.test_pass is True

    def test_refiner_noop_then_more_refine_fails_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                _refine("r1"),
                _refine("r1"),
                _noop(),
                _refine("r1"),  # violation — no escalation between
                scripted_final("refiner loop would spin forever"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(outputs, "escalation.refiner_noop_trigger")
        assert gate.test_pass is False
        assert "refine_scenario" in gate.reason

    def test_refiner_noop_without_any_escalation_fails_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                _refine("r1"),
                _refine("r1"),
                _noop(),
                scripted_final("silently abandoned"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(outputs, "escalation.refiner_noop_trigger")
        assert gate.test_pass is False
        assert "never followed by an escalation" in gate.reason


# ---------------------------------------------------------------------------
# Approval reject
# ---------------------------------------------------------------------------


class TestApprovalRejectReplan:
    """AGENTS.md hard invariant #9: a reject means re-plan, not retry."""

    def test_reject_then_retry_with_different_args_passes_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                _launch("s1", seed=42),
                _reject(
                    "launch_visual_production",
                    {"scene_id": "s1", "seed": 42},
                ),
                _launch("s1", seed=99),  # re-plan with new seed
                scripted_final("re-planned s1"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(
            outputs,
            "escalation.approval_reject_retry_with_different_args",
        )
        assert gate.test_pass is True

    def test_reject_then_retry_with_same_args_fails_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                _launch("s1", seed=42),
                _reject(
                    "launch_visual_production",
                    {"scene_id": "s1", "seed": 42},
                ),
                _launch("s1", seed=42),  # violation — identical args
                scripted_final("bad identical retry"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(
            outputs,
            "escalation.approval_reject_retry_with_different_args",
        )
        assert gate.test_pass is False
        assert "identical args" in gate.reason

    def test_reject_then_escalate_then_same_args_passes_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        # An explicit escalation after the reject counts as a re-plan.
        # The escalation SubAgent may choose to re-run the original
        # call deliberately — that's not a blind retry.
        simulator.with_chat_responses(
            [
                _launch("s1", seed=42),
                _reject(
                    "launch_visual_production",
                    {"scene_id": "s1", "seed": 42},
                ),
                _task_escalation(),
                _launch("s1", seed=42),
                scripted_final("escalation re-ran deliberately"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(
            outputs,
            "escalation.approval_reject_retry_with_different_args",
        )
        assert gate.test_pass is True

    def test_reject_with_no_retry_passes_gate(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        # A reject followed by simply abandoning the call is fine —
        # nothing violates "don't retry with same args" if there's no
        # retry at all.
        simulator.with_chat_responses(
            [
                _launch("s1", seed=42),
                _reject(
                    "launch_visual_production",
                    {"scene_id": "s1", "seed": 42},
                ),
                scripted_final("abandoned s1"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        gate = _gate(
            outputs,
            "escalation.approval_reject_retry_with_different_args",
        )
        assert gate.test_pass is True


# ---------------------------------------------------------------------------
# Clean / irrelevant trajectories
# ---------------------------------------------------------------------------


class TestNoEscalationEvents:
    """A trajectory with no failures / noops / rejects emits ``no_events``."""

    def test_clean_trajectory_emits_no_events(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                _launch("s1"),
                _launch("s2"),
                scripted_final("clean run"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        labels = {o.label for o in outputs}
        assert labels == {"escalation.no_events"}
        assert outputs[0].test_pass is True
