"""OrchestratorSimulator — run the real documentary orchestrator offline.

The simulator is a thin harness around :func:`strands_agents.pipeline.build_orchestrator`
that wires the six fakes from :mod:`strands_agents.sim` into every IO
boundary, hands the agent a scripted chat model, runs it, and returns
the captured trajectory. It lets trajectory tests assert on the real
orchestrator's tool-call sequence against deterministic IO — which is
the composition proof the individual quanta tests (PR-S1) can't give.

Two scripts drive one run:

* The **chat script** — a sequence of :class:`AIMessage` responses fed to
  a :class:`FakeMessagesListChatModel`. Each message either carries
  ``tool_calls`` (telling the orchestrator which tool to invoke next)
  or plain ``content`` (the final answer that stops the agent).
* The **LLM helper script** (:class:`LLMScript`) — what the scripted
  ``FakeLLM`` returns when a tool calls into a helper-injected LLM
  (e.g. ``generate_scenario`` returning a canned scenes list).

Example::

    from strands_agents.sim import (
        OrchestratorSimulator,
        scripted_tool_call,
        scripted_final,
    )
    from strands_agents.sim.llm import LLMScript

    llm_script = LLMScript().when_generate_scenario(
        response={"scenes": [...], "visual_style": {...}, "style_lock": {...}},
    )
    sim = OrchestratorSimulator(llm_script=llm_script)
    sim.with_chat_responses([
        scripted_tool_call("generate_scenario", {
            "topic": "inflation", "num_scenes": 3,
            "style": "docu", "language": "en-US",
        }),
        scripted_final("done"),
    ])
    result = await sim.run("make a 5-minute documentary", tmp_path)
    assert result.trajectory_ops("llm") == ["generate_scenario"]
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from deepagents import SubAgent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from strands_agents.pipeline import build_orchestrator
from strands_agents.sim.llm import LLMScript
from strands_agents.sim.recorder import CallRecord
from strands_agents.sim.substrate import Substrate


_Tool = BaseTool | Callable[..., Any] | dict[str, Any]


class _ScriptedToolCallingModel(FakeMessagesListChatModel):
    """:class:`FakeMessagesListChatModel` that accepts ``bind_tools``.

    The LangGraph tool-calling agent calls ``bind_tools`` on its chat
    model before every LLM step. Vanilla
    :class:`FakeMessagesListChatModel` raises ``NotImplementedError`` in
    ``bind_tools``, which stops the agent before any tool fires.
    Returning ``self`` ignores the binding — acceptable for the
    simulator because the scripted responses already encode the tool
    calls we want the agent to make. Structured-output binding is also
    a no-op for the same reason.
    """

    def bind_tools(
        self,
        tools: Sequence[Any],
        **kwargs: Any,  # noqa: ARG002 — ignored; scripted responses decide
    ) -> Runnable[Any, Any]:
        _ = tools  # unused — the script decides which tools are called
        return self

    def with_structured_output(
        self,
        schema: Any,
        **kwargs: Any,  # noqa: ARG002 — ignored; scripted responses decide
    ) -> Runnable[Any, Any]:
        _ = schema
        return self


# ---------------------------------------------------------------------------
# Chat-script builders
# ---------------------------------------------------------------------------


def scripted_tool_call(
    name: str,
    args: dict[str, Any],
    *,
    call_id: str | None = None,
    content: str = "",
) -> AIMessage:
    """Build an :class:`AIMessage` that invokes one tool.

    Args:
        name: Tool name registered with the orchestrator (must match a
            ``@tool``-decorated function's name).
        args: Keyword arguments for the tool call.
        call_id: Optional deterministic id for the tool call. When
            ``None`` one is generated with a random suffix so callers
            that want reproducible call ids can pass their own.
        content: Optional "thinking" content the model emits alongside
            the tool call. Defaults to empty.

    Returns:
        An :class:`AIMessage` with one ``tool_calls`` entry.
    """
    return AIMessage(
        content=content,
        tool_calls=[
            {
                "name": name,
                "args": dict(args),
                "id": call_id or f"call_{name}_{uuid4().hex[:8]}",
                "type": "tool_call",
            }
        ],
    )


def scripted_parallel_tool_calls(
    *calls: tuple[str, dict[str, Any]],
    content: str = "",
) -> AIMessage:
    """Build an :class:`AIMessage` that invokes several tools in parallel.

    Used to simulate the orchestrator fan-out pattern from AGENTS.md
    "Timing stage" — one ``launch_audio_render`` per scene on one
    turn. Each ``(name, args)`` pair becomes a tool call with a
    generated id.

    Args:
        *calls: Pairs of ``(tool_name, args_dict)``.
        content: Optional accompanying content.

    Returns:
        An :class:`AIMessage` with one ``tool_calls`` entry per pair.
    """
    return AIMessage(
        content=content,
        tool_calls=[
            {
                "name": name,
                "args": dict(args),
                "id": f"call_{name}_{i}_{uuid4().hex[:6]}",
                "type": "tool_call",
            }
            for i, (name, args) in enumerate(calls)
        ],
    )


def scripted_final(content: str) -> AIMessage:
    """Build the terminating :class:`AIMessage`.

    The orchestrator stops when the chat model returns a message with
    no tool calls. Use this as the last entry in a chat script.
    """
    return AIMessage(content=content)


# ---------------------------------------------------------------------------
# Simulation result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimulationResult:
    """Output of one :meth:`OrchestratorSimulator.run`.

    Attributes:
        final_state: The raw mapping returned by
            :meth:`CompiledStateGraph.ainvoke`. Includes the full
            message history, files, and any interrupt payload.
        trajectory: Copy of the :class:`Recorder`'s records at the
            moment :meth:`run` returned. Ordered chronologically.
    """

    final_state: dict[str, Any]
    trajectory: list[CallRecord] = field(default_factory=list)

    def trajectory_ops(self, channel: str | None = None) -> list[str]:
        """Return the ``op`` strings from :attr:`trajectory`.

        Args:
            channel: If given, only ops from that channel.
        """
        return [
            rec.op for rec in self.trajectory if channel is None or rec.channel == channel
        ]

    def ops_count(self, channel: str, op: str) -> int:
        """Count how many times ``op`` was recorded on ``channel``."""
        return sum(
            1 for rec in self.trajectory if rec.channel == channel and rec.op == op
        )


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------


class OrchestratorSimulator:
    """Harness that boots the real orchestrator with fakes plumbed in.

    Lifecycle::

        sim = OrchestratorSimulator(llm_script=...)
        sim.with_chat_responses([...])
        result = await sim.run("brief", tmp_path)
        sim.shutdown()

    The simulator owns one :class:`Substrate`. The substrate's fakes
    are installed for the duration of :meth:`run` and uninstalled on
    exit (even if the agent raises). :meth:`shutdown` must be called
    at the end of the test to release the substrate's real
    ``AsyncTaskPool``.
    """

    def __init__(
        self,
        *,
        llm_script: LLMScript | None = None,
        workers_total: int = 2,
    ) -> None:
        """Create a simulator with a fresh substrate.

        Args:
            llm_script: Script driving the helper-level
                :class:`FakeLLM`. Rules can still be appended after
                construction via ``sim.substrate.llm._script.when_*``.
            workers_total: GPU worker count reported by the renderer's
                ``health_check``. Defaults to 2.
        """
        self.substrate = Substrate(
            llm_script=llm_script,
            workers_total=workers_total,
        )
        self._chat_responses: list[BaseMessage] = []
        self._tools: list[_Tool] | None = None
        self._subagents: list[SubAgent] | None = None
        self._memory: list[str] | None = None
        self._model: BaseChatModel | None = None

    # ------------------------------------------------------------------
    # Builder-style setters
    # ------------------------------------------------------------------

    def with_chat_responses(
        self, responses: Sequence[BaseMessage]
    ) -> OrchestratorSimulator:
        """Register the chat-model script.

        Args:
            responses: Ordered :class:`AIMessage` sequence. Each call
                the orchestrator makes to the chat model consumes one
                entry. The script must end with a message that has no
                ``tool_calls``, otherwise the agent will ask for more
                responses than are available and the run will raise.
        """
        self._chat_responses = list(responses)
        return self

    def with_tools(self, tools: Sequence[_Tool]) -> OrchestratorSimulator:
        """Register the tool list the orchestrator will see.

        The simulator defaults to an empty list — the same default
        :func:`build_orchestrator` uses. Tests that want to exercise
        real tool dispatch (against the substrate fakes) pass them
        here. Tools must be callable in LangChain's tool protocol
        (``@langchain_core.tools.tool`` decorators, :class:`BaseTool`
        subclasses, or plain JSON-Schema dicts with a matching
        callable).
        """
        self._tools = list(tools)
        return self

    def with_subagents(
        self, subagents: Sequence[SubAgent]
    ) -> OrchestratorSimulator:
        """Override the default SubAgent list (empty by default)."""
        self._subagents = list(subagents)
        return self

    def with_memory(self, memory: Sequence[str]) -> OrchestratorSimulator:
        """Override the memory-file path list.

        When unset, the pipeline default is used (AGENTS.md + deployment
        memory, resolved relative to ``run_dir``).
        """
        self._memory = list(memory)
        return self

    def with_model(self, model: BaseChatModel) -> OrchestratorSimulator:
        """Override the chat model.

        By default :meth:`run` builds a
        :class:`FakeMessagesListChatModel` from the chat responses.
        Tests that want a custom chat model (e.g. one that records
        prompts) can inject it here — ``with_chat_responses`` is
        ignored when a custom model is set.
        """
        self._model = model
        return self

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def _build_model(self) -> BaseChatModel:
        if self._model is not None:
            return self._model
        if not self._chat_responses:
            msg = (
                "no chat responses scripted — call with_chat_responses(...) "
                "or with_model(...) before run()"
            )
            raise ValueError(msg)
        return _ScriptedToolCallingModel(responses=self._chat_responses)

    async def run(
        self,
        brief: str,
        run_dir: Path,
    ) -> SimulationResult:
        """Run the orchestrator end-to-end with fakes installed.

        Builds the chat model from the scripted responses, installs
        the substrate, calls :func:`build_orchestrator`, invokes the
        resulting agent with ``brief`` as the user message, and
        returns the final state plus recorded trajectory.

        Args:
            brief: User brief message (topic + duration + language).
            run_dir: Filesystem root for the orchestrator's
                :class:`FilesystemBackend`. AGENTS.md and scratch
                files live here.

        Returns:
            A :class:`SimulationResult` carrying the agent's final
            state and the captured trajectory.
        """
        model = self._build_model()
        tools: list[_Tool] = list(self._tools) if self._tools is not None else []
        subagents = self._subagents if self._subagents is not None else []

        with self.substrate.installed():
            build_kwargs: dict[str, Any] = {
                "model": model,
                "tools": tools,
                "subagents": subagents,
            }
            if self._memory is not None:
                build_kwargs["memory"] = self._memory

            agent = build_orchestrator(run_dir, **build_kwargs)
            final_state: dict[str, Any] = await agent.ainvoke(
                {"messages": [("user", brief)]}
            )

        return SimulationResult(
            final_state=final_state,
            trajectory=self.substrate.recorder.records,
        )

    def shutdown(self) -> None:
        """Release the underlying :class:`AsyncTaskPool`.

        Call at the end of each test (e.g. from a pytest fixture's
        teardown) to avoid leaking worker threads between tests.
        """
        self.substrate.shutdown()


__all__ = [
    "OrchestratorSimulator",
    "SimulationResult",
    "scripted_final",
    "scripted_parallel_tool_calls",
    "scripted_tool_call",
]
