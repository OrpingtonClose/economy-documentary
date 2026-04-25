"""Live pipeline runner — slice 9a of the documentary migration.

A sibling of :class:`strands_agents.playground.pipeline_adapter.SimulatedPipelineRun`
that drives the **real** ``create_deep_agent`` orchestrator
(:mod:`strands_agents.pipeline`) end-to-end onto a playground
:class:`RunStream`.

The simulator emits a hand-rolled, deterministic event sequence so the
``/pipeline`` UI surface can be exercised without LLM tokens. The live
runner instead **observes** the real agent as it executes — every tool
the LLM picks, every interrupt the orchestrator hits, every assembly
artifact — and translates those observations into the same
``pipeline.*`` event vocabulary the simulator emits, so the UI does not
need to know which engine produced the events.

The runner is built on three primitives, in order of increasing
responsibility:

* :class:`_StageTracker` — pure state machine that maps the *current*
  tool name to the orchestrator's five canonical stages
  (``scenario`` → ``audio`` → ``visual`` → ``production`` →
  ``assembly``) and emits ``pipeline.stage_started`` /
  ``pipeline.stage_finished`` brackets at transitions. Closes any
  open stage on terminal so the trajectory is well-formed.

* :class:`_PipelineCallbackHandler` — a LangChain
  :class:`~langchain_core.callbacks.AsyncCallbackHandler` that
  subscribes to ``on_tool_start`` / ``on_tool_end`` and emits
  ``pipeline.tool_call_started`` / ``pipeline.tool_call_finished``
  events through :func:`translate_pipeline_event`. Drives the
  stage tracker on every tool call.

* :class:`LivePipelineRun` — the public surface. Wraps the orchestrator
  build, the callback wiring, and an interrupt-resolution loop that
  emits ``pipeline.approval_gate`` + ``pipeline.approval_resumed``
  envelopes around every gate. Mirrors :class:`SimulatedPipelineRun`'s
  return shape so the FastAPI dispatcher can accept either one.

Design contract (versus :class:`SimulatedPipelineRun`):

* The runner **does not** invent stage transitions — they fall out of
  observed tool calls. A run that picks tools the orchestrator did
  not declare still emits a (degraded) ``pipeline.tool_call_*``
  bracket plus an ``unknown`` stage; nothing is dropped.

* The runner **does** auto-resolve interrupts (default: ``accept``) so
  a run started from the playground completes without a human
  operator. Tests inject a different ``operator_decision`` to
  exercise reject / respond paths. The default keeps the demo
  surface honest about what the pipeline expects (gates pause, the
  operator decides).

* The runner **does not** swallow LLM exceptions. A real failure
  surfaces as an exception out of :meth:`run` so the dispatcher in
  :mod:`server.playground` lands a ``run.error`` terminal — the same
  way the c01..c15 component dispatcher behaves.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.types import Command

from strands_agents.playground.events import RunStream
from strands_agents.playground.pipeline_adapter import (
    APPROVAL_GATES,
    PIPELINE_STAGES,
    translate_pipeline_event,
)
from strands_agents.run import _extract_interrupt_metadata

logger = logging.getLogger(__name__)


#: Tool-name → stage mapping. Mirrors the orchestrator's tool catalog
#: in :mod:`strands_agents.pipeline`. ``check_tasks`` and
#: ``await_tasks`` are stage-neutral: they never trigger a stage
#: transition.
_TOOL_TO_STAGE: dict[str, str | None] = {
    "generate_scenario": "scenario",
    "evaluate_scenario": "scenario",
    "refine_scenario": "scenario",
    "evaluate_timing": "audio",
    "launch_audio_render": "audio",
    "content_analyst": "visual",
    "visual_concepter": "visual",
    "launch_visual_production": "production",
    "launch_assembly": "assembly",
    "launch_b2_sync": "assembly",
    "check_tasks": None,
    "await_tasks": None,
    # ``request_human_approval`` is an escalation gate; not part of
    # any of the five canonical stages.
    "request_human_approval": None,
}


def _stage_for_tool(tool_name: str) -> str | None:
    """Look up the canonical stage for a tool name.

    Unknown tools return ``None`` (do not transition the stage). The
    runner never raises on an unknown tool — see the "nothing is
    dropped" invariant in the module docstring.
    """
    return _TOOL_TO_STAGE.get(tool_name)


def _topic_slug(topic: str) -> str:
    """B2-friendly slug for the topic. Matches the simulator's slug."""
    return topic.replace(" ", "_") or "documentary"


@dataclass
class _StageTracker:
    """Tracks the open pipeline stage and emits open/close events.

    The DeepAgent has no per-stage node — stages are an
    interpretation we layer over its tool-call stream. This tracker
    is the only place that interpretation lives so the runner stays
    declarative.

    Attributes:
        stream: The :class:`RunStream` to emit translated events
            onto. Stage events go through
            :func:`translate_pipeline_event` so they share the
            simulator's wire shape.
        current: Currently open stage, or ``None`` when no stage
            bracket is open. Mutated by :meth:`transition_to` and
            :meth:`close_open`.
        started_at: Wall-clock start of the open stage; used to
            stamp ``elapsed_ms`` on the close event.
    """

    stream: RunStream
    current: str | None = None
    started_at: float = 0.0

    async def transition_to(self, target: str | None) -> None:
        """Open ``target`` if needed, closing the current stage first.

        ``target=None`` is a no-op (stage-neutral tool). Repeated
        transitions to the same stage are also no-ops — the
        scenario stage stays open across
        ``generate_scenario`` → ``evaluate_scenario`` → ``refine_scenario``.
        """
        if target is None:
            return
        if target == self.current:
            return
        await self.close_open()
        await _emit_translated(
            self.stream,
            "pipeline.stage_started",
            {"stage": target},
        )
        self.current = target
        self.started_at = time.perf_counter()

    async def close_open(self) -> None:
        """Close the currently open stage, if any."""
        if self.current is None:
            return
        elapsed_ms = int((time.perf_counter() - self.started_at) * 1000)
        await _emit_translated(
            self.stream,
            "pipeline.stage_finished",
            {"stage": self.current, "elapsed_ms": elapsed_ms},
        )
        self.current = None
        self.started_at = 0.0


async def _emit_translated(
    stream: RunStream,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Translate an orchestrator event and emit it on ``stream``.

    Centralises the translator call so every event the runner
    produces shares the same pipeline_adapter contract. A test that
    asserts on the simulator's emit-shape automatically covers the
    live runner's emit-shape too.
    """
    translated = translate_pipeline_event(event_type, data)
    await stream.emit(
        kind=translated.kind,
        summary=translated.summary,
        detail=translated.detail,
    )


class _PipelineCallbackHandler(AsyncCallbackHandler):
    """LangChain async callback that forwards tool events to the stream.

    LangGraph's ``ToolNode`` invokes registered callbacks around
    every tool call, including those routed through deepagents'
    ``HumanInTheLoopMiddleware`` (after the interrupt is resolved).
    This handler turns each pair into the orchestrator's
    ``pipeline.tool_call_started`` / ``pipeline.tool_call_finished``
    vocabulary and drives the stage tracker.

    The handler is **defensive**: any exception inside a callback is
    logged and swallowed so a buggy emit cannot break the run it is
    observing.
    """

    def __init__(
        self,
        stream: RunStream,
        stage_tracker: _StageTracker,
    ) -> None:
        self._stream = stream
        self._stage = stage_tracker
        #: Maps run_id (LangChain UUID) → wall-clock start so the
        #: ``elapsed_ms`` on the finish event matches the actual
        #: tool execution time. LangChain hands the same run_id to
        #: matching start / end pairs.
        self._starts: dict[str, float] = {}
        #: Maps run_id → tool name. ``on_tool_end`` does not always
        #: receive ``name`` in its kwargs (langchain version skew),
        #: so we cache it at start.
        self._tool_names: dict[str, str] = {}
        #: Monotonic count of every ``on_tool_start`` observed across
        #: the run, including those that later errored. This is the
        #: number reported as ``tool_call_count`` in the terminal
        #: payload — it is the total tools observed, not the
        #: still-in-flight count (which is exposed separately via
        #: :attr:`inflight_tool_count`).
        self._total_calls: int = 0

    @property
    def total_calls(self) -> int:
        """Total tool-start callbacks observed during this run."""
        return self._total_calls

    @property
    def inflight_tool_count(self) -> int:
        """Tools still open at the moment of inspection.

        Healthy runs settle this at 0 by the time :meth:`run`
        returns; a non-zero value indicates a callback leak (start
        without a matching end / error).
        """
        return len(self._starts)

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Open a stage bracket (if needed) and emit a tool-start event."""
        try:
            tool_name = str(serialized.get("name") or "unknown")
            self._starts[str(run_id)] = time.perf_counter()
            self._tool_names[str(run_id)] = tool_name
            self._total_calls += 1
            await self._stage.transition_to(_stage_for_tool(tool_name))
            await _emit_translated(
                self._stream,
                "pipeline.tool_call_started",
                {
                    "tool": tool_name,
                    "agent": "orchestrator",
                    "args_summary": input_str[:160],
                },
            )
        except Exception:  # noqa: BLE001 — emit must never break the run
            logger.debug("on_tool_start emit failed", exc_info=True)

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Emit a tool-finish event with the observed latency."""
        try:
            tool_name = self._tool_name_for(run_id, kwargs)
            start = self._starts.pop(str(run_id), None)
            elapsed_ms = (
                int((time.perf_counter() - start) * 1000) if start is not None else -1
            )
            await _emit_translated(
                self._stream,
                "pipeline.tool_call_finished",
                {
                    "tool": tool_name,
                    "agent": "orchestrator",
                    "elapsed_ms": elapsed_ms,
                    "ok": True,
                },
            )
        except Exception:  # noqa: BLE001 — emit must never break the run
            logger.debug("on_tool_end emit failed", exc_info=True)

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Emit a failed tool-finish so the trajectory carries the error."""
        try:
            tool_name = self._tool_name_for(run_id, kwargs)
            start = self._starts.pop(str(run_id), None)
            elapsed_ms = (
                int((time.perf_counter() - start) * 1000) if start is not None else -1
            )
            await _emit_translated(
                self._stream,
                "pipeline.tool_call_finished",
                {
                    "tool": tool_name,
                    "agent": "orchestrator",
                    "elapsed_ms": elapsed_ms,
                    "ok": False,
                    "error_class": type(error).__name__,
                    "error": str(error)[:200],
                },
            )
        except Exception:  # noqa: BLE001 — emit must never break the run
            logger.debug("on_tool_error emit failed", exc_info=True)

    def _tool_name_for(self, run_id: UUID, kwargs: dict[str, Any]) -> str:
        """Recover the tool name on the end-side callback.

        Prefers the run-id cache populated at ``on_tool_start``; falls
        back to whatever LangChain stamps on the end-side kwargs;
        falls back to ``"unknown"`` rather than guessing.
        """
        cached = self._tool_names.pop(str(run_id), None)
        if cached:
            return cached
        name = kwargs.get("name")
        if name:
            return str(name)
        return "unknown"


@dataclass
class LivePipelineRun:
    """Drive the real ``create_deep_agent`` orchestrator onto a stream.

    Mirrors :class:`SimulatedPipelineRun`'s public surface so the
    FastAPI dispatcher can accept either runner under the same
    request shape. Differences:

    * ``agent``: the pre-built compiled LangGraph (or anything
      with ``.ainvoke``). The dispatcher in :mod:`server.playground`
      builds one with :func:`build_orchestrator` and the project's
      default tools / subagents; tests inject a stub graph.

    * ``operator_decision``: async callable invoked once per
      interrupt. Default :func:`auto_accept_interrupt` returns
      ``Command(resume={"type": "accept"})`` — the playground UI's
      "happy path" demo. Tests can pass a scripted handler to
      exercise reject / respond / edit.

    * ``max_interrupt_rounds``: safety cap; mirrors
      :func:`run_documentary` so a misbehaving graph cannot loop
      forever inside the dispatcher.

    Attributes:
        topic: User-supplied documentary topic (echoed in the
            ``run_started`` event).
        target_duration_sec: Target final video length.
        language: BCP-47 language code.
        agent: Pre-built compiled agent. The runner drives it via
            ``ainvoke`` and resumes interrupts with
            :class:`Command`.
        run_dir: Filesystem root the agent reads / writes scratch
            files into. Used only for surfacing in the ``terminal``
            payload.
        operator_decision: Resolves each interrupt to a
            :class:`Command`. Default: auto-accept.
        max_interrupt_rounds: Safety cap on the number of resumes.
        per_event_delay_s: Optional sleep after every event so the
            UI sees a progress timeline. Default 0.0 (no delay).
    """

    topic: str
    target_duration_sec: int
    language: str
    agent: Any
    run_dir: Path
    operator_decision: "OperatorDecision | None" = None
    max_interrupt_rounds: int = 32
    per_event_delay_s: float = 0.0

    async def run(self, stream: RunStream) -> dict[str, Any]:
        """Drive the orchestrator to completion and return a terminal dict.

        Emits one ``pipeline.run_started`` at entry, observes every
        tool call + interrupt, and emits one
        ``pipeline.run_finished`` (or a ``stage_failed`` followed by
        ``run_finished`` with ``status=error``) on exit.
        """
        operator = self.operator_decision or auto_accept_interrupt
        tracker = _StageTracker(stream=stream)
        handler = _PipelineCallbackHandler(stream, tracker)
        # ``thread_id`` is required when the agent is compiled with a
        # checkpointer (which it must be for ``Command(resume=...)``
        # to work). Each run uses its own thread id so concurrent
        # runs against the same checkpointer cannot collide.
        config: dict[str, Any] = {
            "callbacks": [handler],
            "configurable": {"thread_id": f"pipeline-live-{id(self)}"},
        }

        await _emit_translated(
            stream,
            "pipeline.run_started",
            {
                "topic": self.topic,
                "target_duration_sec": self.target_duration_sec,
                "language": self.language,
            },
        )

        gate_count = 0
        tool_count = 0
        started_perf = time.perf_counter()
        final_mp4_b2_url: str | None = None
        status = "ok"

        try:
            initial = {
                "messages": [("user", _initial_brief(self))],
            }
            state = await self.agent.ainvoke(initial, config=config)

            rounds = 0
            while isinstance(state, dict) and state.get("__interrupt__"):
                rounds += 1
                if rounds > self.max_interrupt_rounds:
                    raise RuntimeError(
                        "interrupt loop exceeded max rounds "
                        f"({self.max_interrupt_rounds})"
                    )
                interrupt_id, tool_name, payload = _extract_hitl_interrupt(state)
                gate_count += 1
                await _emit_translated(
                    stream,
                    "pipeline.approval_gate",
                    {
                        "gate_name": tool_name,
                        "allowed_decisions": payload.get("allowed_decisions")
                        or _default_allowed_for(tool_name),
                        "interrupt_id": interrupt_id,
                    },
                )
                command = await operator(state)
                decision = _decision_from_command(command)
                await _emit_translated(
                    stream,
                    "pipeline.approval_resumed",
                    {
                        "gate_name": tool_name,
                        "decision": decision,
                        "interrupt_id": interrupt_id,
                    },
                )
                if self.per_event_delay_s > 0:
                    await asyncio.sleep(self.per_event_delay_s)
                state = await self.agent.ainvoke(command, config=config)

            await tracker.close_open()
            tool_count = handler.total_calls
            final_mp4_b2_url = _scrape_final_mp4_url(state)
            await _emit_translated(
                stream,
                "pipeline.run_finished",
                {
                    "status": "ok",
                    "final_mp4_b2_url": final_mp4_b2_url
                    or _placeholder_mp4_url(self.topic),
                },
            )
        except Exception as exc:
            status = "error"
            #: Capture the open stage *before* ``close_open`` clears it
            #: so the ``stage_failed`` envelope carries the real stage
            #: name. The frontend only highlights stages whose name is
            #: in :data:`PIPELINE_STAGES`; emitting ``"unknown"`` would
            #: make the failure invisible in the ribbon.
            failed_stage = tracker.current or "unknown"
            await tracker.close_open()
            await _emit_translated(
                stream,
                "pipeline.stage_failed",
                {
                    "stage": failed_stage,
                    "reason": f"{type(exc).__name__}: {exc}",
                },
            )
            await _emit_translated(
                stream,
                "pipeline.run_finished",
                {
                    "status": "error",
                    "error_class": type(exc).__name__,
                    "error": str(exc)[:400],
                },
            )
            raise

        elapsed_ms = int((time.perf_counter() - started_perf) * 1000)
        return {
            "status": status,
            "topic": self.topic,
            "target_duration_sec": self.target_duration_sec,
            "language": self.language,
            "stage_count": len(PIPELINE_STAGES),
            "approval_gate_count": gate_count,
            "tool_call_count": tool_count,
            "inflight_tool_count": handler.inflight_tool_count,
            "event_count": len(stream.snapshot()),
            "elapsed_ms": elapsed_ms,
            "final_mp4_b2_url": final_mp4_b2_url,
            "run_dir": str(self.run_dir),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Async resume handler: takes the current graph state, returns a
# :class:`Command` to drive the next ``ainvoke``. Same shape as
# :data:`strands_agents.run.OperatorDecision`.
OperatorDecision = Any


async def auto_accept_interrupt(state: dict[str, Any]) -> Command:
    """Default resume handler: approve every pending interrupt.

    The langchain ``HumanInTheLoopMiddleware`` (which deepagents
    wires under :func:`build_orchestrator`) expects
    ``Command(resume={"decisions": [...]})`` with one entry per
    interrupted tool call, using the middleware's vocabulary
    (``approve`` / ``edit`` / ``reject``). The playground demo
    always picks ``approve`` so the happy path runs end-to-end.
    """
    interrupts = state.get("__interrupt__", []) or []
    count = max(1, len(interrupts))
    return Command(
        resume={"decisions": [{"type": "approve"} for _ in range(count)]},
    )


def _default_allowed_for(tool_name: str) -> list[str]:
    """Conservative fallback when an interrupt payload omits ``allowed_decisions``."""
    if tool_name in APPROVAL_GATES:
        return ["accept", "edit", "reject", "respond"]
    return ["accept", "reject", "respond"]


def _decision_from_command(command: Command) -> str:
    """Recover the decision ``type`` from a resume :class:`Command`.

    Supports two shapes:

    * The langchain ``HumanInTheLoopMiddleware`` shape:
      ``resume={"decisions": [{"type": "approve"}, …]}``. The first
      decision wins for reporting purposes (the demo always emits
      a single decision per interrupt, matching the action-request
      count).
    * The legacy single-decision shape:
      ``resume={"type": "accept", …}``.

    Falls back to ``"respond"`` when neither shape matches so the
    UI never renders ``None``.
    """
    resume = getattr(command, "resume", None)
    if isinstance(resume, dict):
        decisions = resume.get("decisions")
        if isinstance(decisions, list) and decisions:
            first = decisions[0]
            if isinstance(first, dict):
                t = first.get("type")
                if isinstance(t, str):
                    return t
        decision = resume.get("type")
        if isinstance(decision, str):
            return decision
    return "respond"


def _extract_hitl_interrupt(
    state: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Return ``(interrupt_id, tool_name, payload)`` for a HITL interrupt.

    The langchain ``HumanInTheLoopMiddleware`` stores the interrupt
    value as ``{"action_requests": [...], "review_configs": [...]}``.
    Falls back to the legacy
    :func:`strands_agents.run._extract_interrupt_metadata` shape
    when no ``action_requests`` are present, so the runner stays
    compatible with project-native interrupts (e.g. the escalation
    SubAgent's ``request_human_approval`` shape).
    """
    interrupts = state.get("__interrupt__", []) or []
    if not interrupts:
        raise RuntimeError("run loop saw no pending interrupt")
    interrupt = interrupts[0]
    value = (
        interrupt.get("value")
        if isinstance(interrupt, dict)
        else getattr(interrupt, "value", {}) or {}
    )
    if not isinstance(value, dict):
        value = {}

    interrupt_id: str | None = (
        interrupt.get("id")
        if isinstance(interrupt, dict)
        else getattr(interrupt, "id", None)
    )

    action_requests = value.get("action_requests")
    if isinstance(action_requests, list) and action_requests:
        first = action_requests[0]
        if isinstance(first, dict):
            tool_name = str(first.get("name") or "unknown")
            args = first.get("args") or {}
            review_configs = value.get("review_configs") or []
            allowed: list[str] | None = None
            if isinstance(review_configs, list):
                for rc in review_configs:
                    if isinstance(rc, dict) and rc.get("action_name") == tool_name:
                        ad = rc.get("allowed_decisions")
                        if isinstance(ad, list):
                            allowed = [str(d) for d in ad]
                            break
            return (
                str(interrupt_id) if interrupt_id else "",
                tool_name,
                {
                    "args": args if isinstance(args, dict) else {},
                    "description": first.get("description"),
                    "allowed_decisions": allowed,
                },
            )

    return _extract_interrupt_metadata(state)


def _initial_brief(run: LivePipelineRun) -> str:
    """Format the user-facing brief the orchestrator receives.

    Kept in one place so a future schema change (e.g. adding
    ``style_lock``) lives next to the runner — the dispatcher does
    not need to know the brief shape.
    """
    return (
        f"Topic: {run.topic}\n"
        f"Target duration (sec): {run.target_duration_sec}\n"
        f"Language: {run.language}\n"
        f"Pipeline shape: scenario → audio → visual → production → "
        f"assembly. Approval gates fire on launch_visual_production, "
        f"launch_assembly, request_human_approval."
    )


def _scrape_final_mp4_url(state: Any) -> str | None:
    """Best-effort recover the final MP4 URL from the graph terminal.

    The orchestrator's final ``messages`` list is the only stable
    artifact channel: the assembly tool returns ``{ "b2_url": …,
    "kind": "master_mp4" }`` and the agent quotes it in its final
    user-facing message. Tests inject the URL directly via the stub
    graph; production runs will populate it via the real assembly
    tool when slice 9b lands.
    """
    if not isinstance(state, dict):
        return None
    final = state.get("final_mp4_b2_url")
    if isinstance(final, str) and final:
        return final
    artifact = state.get("master_mp4")
    if isinstance(artifact, dict):
        url = artifact.get("b2_url")
        if isinstance(url, str) and url:
            return url
    # Fall back to scanning the message transcript: tool messages from
    # ``launch_b2_sync`` and ``launch_assembly`` carry the URL in
    # their content, and the final AIMessage typically quotes it.
    messages = state.get("messages")
    if isinstance(messages, list):
        url_pattern = re.compile(r"b2://[A-Za-z0-9_./\-]+\.mp4")
        for message in reversed(messages):
            content = getattr(message, "content", None)
            if isinstance(content, str):
                match = url_pattern.search(content)
                if match:
                    return match.group(0)
    return None


def _placeholder_mp4_url(topic: str) -> str:
    """Stable placeholder URL while real B2 sync is not wired (slice 9b)."""
    return f"b2://documentary/{_topic_slug(topic)}/master_mp4/r0001.mp4"


def handler_call_count(handler: _PipelineCallbackHandler) -> int:
    """Total tool-start callbacks observed during the run.

    Thin alias over :attr:`_PipelineCallbackHandler.total_calls`,
    kept so external test helpers that imported the function name
    keep working. Use the attribute directly inside the runner.
    """
    return handler.total_calls


__all__ = [
    "LivePipelineRun",
    "OperatorDecision",
    "auto_accept_interrupt",
]
