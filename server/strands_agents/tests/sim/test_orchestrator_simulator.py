"""Direct-proof tests for :class:`OrchestratorSimulator`.

The simulator's contract, proven here:

* :meth:`run` boots the real :func:`build_orchestrator` — a
  ``create_deep_agent`` call with real memory middleware, real
  backend, real interrupt config — not a mock orchestrator.
* Scripted ``AIMessage`` responses drive the agent step-by-step;
  the agent terminates when a scripted message has no tool calls.
* Tools registered via :meth:`with_tools` execute exactly once per
  scripted ``tool_call``. Their side effects (calls into the
  substrate) show up in the shared :class:`Recorder`.
* Parallel tool calls on one chat turn all execute before the next
  chat turn.
* The substrate is **installed** while the agent runs and
  **uninstalled** when :meth:`run` returns — on both the happy path
  and when the agent raises.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from langchain_core.tools import tool

from strands_agents import scenario_agent
from strands_agents.scenario_agent import generate_scenario
from strands_agents.sim import (
    LLMScript,
    OrchestratorSimulator,
    SimulationResult,
    scripted_final,
    scripted_parallel_tool_calls,
    scripted_tool_call,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Clean run directory for each test."""
    return tmp_path


@pytest.fixture
def simulator() -> Iterator[OrchestratorSimulator]:
    sim = OrchestratorSimulator()
    try:
        yield sim
    finally:
        sim.shutdown()


def _run(sim: OrchestratorSimulator, brief: str, run_dir: Path) -> SimulationResult:
    """Synchronous wrapper so tests don't need pytest-asyncio."""
    return asyncio.run(sim.run(brief, run_dir))


# ---------------------------------------------------------------------------
# Helper tool: plain echo, no substrate IO. Used to prove the
# chat-script-driven dispatch path works on its own.
# ---------------------------------------------------------------------------


@tool
def record_only(tag: str) -> str:
    """Trivial tool that just echoes its input."""
    return f"recorded:{tag}"


@tool
def always_raises() -> str:
    """Tool that raises unconditionally. Used to force an error path
    through the agent so teardown behavior can be exercised."""
    raise RuntimeError("scripted tool failure")


def _scenario_helpers_registered() -> bool:
    """Return whether the scenario module has an injected generator.

    The teardown tests use this to prove the substrate uninstalled
    itself, rather than poking at the real agent tool (which would
    require a live model to round-trip through).
    """
    return scenario_agent._GENERATOR is not None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSimulatorBoot:
    def test_empty_tools_terminates_on_final_message(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses([scripted_final("done")])
        result = _run(simulator, "irrelevant brief", run_dir)
        # 1 user message + 1 AI final message
        assert len(result.final_state["messages"]) == 2
        assert result.final_state["messages"][-1].content == "done"
        assert result.trajectory == []

    def test_substrate_is_uninstalled_after_run(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses([scripted_final("done")])
        assert not _scenario_helpers_registered()
        _run(simulator, "brief", run_dir)
        # Substrate uninstalled every helper, so the module globals
        # are back to their unconfigured default.
        assert not _scenario_helpers_registered()

    def test_substrate_is_uninstalled_even_on_agent_error(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        # Register a tool that raises on invocation, then script the
        # agent to call it. The agent surfaces the exception and run()
        # propagates it — but teardown must still happen.
        simulator.with_tools([always_raises])
        simulator.with_chat_responses(
            [scripted_tool_call("always_raises", {}), scripted_final("unreachable")]
        )
        with pytest.raises(RuntimeError, match="scripted tool failure"):
            _run(simulator, "brief", run_dir)
        assert not _scenario_helpers_registered()


class TestSimulatorDispatch:
    def test_scripted_tool_call_runs_and_hits_substrate(
        self,
        run_dir: Path,
    ) -> None:
        # Fresh simulator seeded with an LLM script so the real
        # generate_scenario tool has something to return.
        sim = OrchestratorSimulator(
            llm_script=LLMScript().when_generate_scenario(
                response={
                    "scenes": [{"id": "s1"}],
                    "visual_style": {},
                    "style_lock": {},
                    "revision": "r0",
                },
            ),
        )
        try:
            sim.with_tools([generate_scenario]).with_chat_responses(
                [
                    scripted_tool_call(
                        "generate_scenario",
                        {
                            "topic": "inflation",
                            "num_scenes": 1,
                            "style": "docu",
                            "language": "en-US",
                        },
                    ),
                    scripted_final("done"),
                ]
            )
            result = _run(sim, "brief", run_dir)
        finally:
            sim.shutdown()

        # The substrate recorded exactly one scenario-generator call.
        assert result.trajectory_ops("llm") == ["generate_scenario"]
        # And the tool's return value made it back to the agent state.
        tool_messages = [
            m
            for m in result.final_state["messages"]
            if getattr(m, "type", None) == "tool"
        ]
        assert len(tool_messages) == 1
        assert "s1" in tool_messages[0].content

    def test_parallel_tool_calls_all_execute(
        self,
        run_dir: Path,
    ) -> None:
        sim = OrchestratorSimulator(
            llm_script=LLMScript().when_generate_scenario(
                response={
                    "scenes": [{"id": "s1"}],
                    "visual_style": {},
                    "style_lock": {},
                },
                reusable=True,
            ),
        )
        try:
            sim.with_tools([generate_scenario]).with_chat_responses(
                [
                    scripted_parallel_tool_calls(
                        (
                            "generate_scenario",
                            {
                                "topic": "a",
                                "num_scenes": 1,
                                "style": "s",
                                "language": "en-US",
                            },
                        ),
                        (
                            "generate_scenario",
                            {
                                "topic": "b",
                                "num_scenes": 1,
                                "style": "s",
                                "language": "en-US",
                            },
                        ),
                        (
                            "generate_scenario",
                            {
                                "topic": "c",
                                "num_scenes": 1,
                                "style": "s",
                                "language": "en-US",
                            },
                        ),
                    ),
                    scripted_final("done"),
                ]
            )
            result = _run(sim, "brief", run_dir)
        finally:
            sim.shutdown()

        assert result.ops_count("llm", "generate_scenario") == 3

    def test_tool_with_no_substrate_io_still_works(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_tools([record_only]).with_chat_responses(
            [
                scripted_tool_call("record_only", {"tag": "x"}),
                scripted_final("done"),
            ]
        )
        result = _run(simulator, "brief", run_dir)
        # Empty substrate trajectory — the tool did no IO.
        assert result.trajectory == []
        tool_messages = [
            m
            for m in result.final_state["messages"]
            if getattr(m, "type", None) == "tool"
        ]
        assert len(tool_messages) == 1
        assert tool_messages[0].content == "recorded:x"


class TestSimulatorShape:
    def test_requires_a_chat_script_or_model(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        # No with_chat_responses / with_model call → run() must refuse.
        with pytest.raises(ValueError, match="no chat responses"):
            _run(simulator, "brief", run_dir)

    def test_trajectory_is_chronological(
        self,
        run_dir: Path,
    ) -> None:
        sim = OrchestratorSimulator(
            llm_script=(
                LLMScript()
                .when_generate_scenario(
                    response={
                        "scenes": [{"id": "a"}],
                        "visual_style": {},
                        "style_lock": {},
                    }
                )
                .when_generate_scenario(
                    response={
                        "scenes": [{"id": "b"}],
                        "visual_style": {},
                        "style_lock": {},
                    }
                )
            ),
        )
        try:
            sim.with_tools([generate_scenario]).with_chat_responses(
                [
                    scripted_tool_call(
                        "generate_scenario",
                        {
                            "topic": "first",
                            "num_scenes": 1,
                            "style": "s",
                            "language": "en-US",
                        },
                    ),
                    scripted_tool_call(
                        "generate_scenario",
                        {
                            "topic": "second",
                            "num_scenes": 1,
                            "style": "s",
                            "language": "en-US",
                        },
                    ),
                    scripted_final("done"),
                ]
            )
            result = _run(sim, "brief", run_dir)
        finally:
            sim.shutdown()

        assert result.trajectory_ops("llm") == [
            "generate_scenario",
            "generate_scenario",
        ]
