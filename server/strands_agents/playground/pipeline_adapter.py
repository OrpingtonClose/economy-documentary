"""Pipeline playground adapter — slice 7 of the documentary migration.

Bridges the documentary orchestrator's event surface into the
playground's :class:`strands_agents.playground.events.RunStream` so a
single pipeline run is user-auditable from the same `/components/<id>`
workbench that drives c01..c15.

Two concerns, kept deliberately separate so each is testable in
isolation:

* :func:`translate_pipeline_event` — a **pure function** that maps one
  ``(source, event_type, data)`` triple from the orchestrator's event
  vocabulary onto a ``(kind, summary, detail)`` triple the playground
  stream can emit directly. Never touches the network, never touches
  a stream. Shipped as the strands-evals task surface for
  `infra_pipeline_adapter`.

* :func:`generate_simulation_events` + :func:`replay_events_onto_stream`
  — **deterministic event-fixture helpers** used by the strands-evals
  ``infra_pipeline_adapter`` experiment to feed the translator a
  canned sequence in the real wire shape. They are test fixtures
  only; the ``/playground/pipeline/runs`` FastAPI surface always
  drives the real orchestrator (no scripted-replay fallback).

Event vocabulary (the source side of the contract):

==============================  =========================================
Orchestrator source              Shape
==============================  =========================================
``pipeline.run_started``         ``{topic, target_duration_sec, language}``
``pipeline.stage_started``       ``{stage, scene_count?}``
``pipeline.stage_finished``      ``{stage, elapsed_ms}``
``pipeline.tool_call_started``   ``{tool, agent, args_summary}``
``pipeline.tool_call_finished``  ``{tool, agent, elapsed_ms, ok}``
``pipeline.approval_gate``       ``{gate_name, allowed_decisions}``
``pipeline.approval_resumed``    ``{gate_name, decision}``
``pipeline.artifact``            ``{kind, scene_num?, revision_tag, b2_url?}``
``pipeline.stage_failed``        ``{stage, reason, detail}``
``pipeline.run_finished``        ``{status, final_mp4_b2_url?}``
==============================  =========================================

Playground kinds (the sink side):

* ``pipeline.run_started`` / ``pipeline.run_finished`` —
  first / last stream events.
* ``pipeline.stage.<name>.start`` /
  ``pipeline.stage.<name>.end`` — per-stage bracket so the UI can
  render a five-segment ribbon.
* ``pipeline.tool.<tool>.start`` / ``pipeline.tool.<tool>.end`` —
  tool-call granularity; matches the AG-UI ``TOOL_CALL_START`` /
  ``TOOL_CALL_END`` semantics already used by c01..c15.
* ``pipeline.artifact`` — artifact produced and (optionally)
  checkpointed to B2. Detail carries the revision tag so the UI can
  honour invariant 8 without asking the orchestrator.
* ``pipeline.approval.waiting`` / ``pipeline.approval.resumed`` —
  human-in-the-loop pauses. ``waiting`` is a terminal-for-now event
  when the orchestrator yields; ``resumed`` fires when the operator
  responds.
* ``pipeline.stage_failed`` — a stage hit a failure after the
  orchestrator's own retry envelope. Always carries a reason.

Unknown orchestrator events translate to ``pipeline.unknown`` with
the original ``event_type`` preserved in ``detail`` — the adapter
**never drops** events. A refiner that gains a new event tomorrow
surfaces as "unknown" today rather than silently disappearing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


#: Ordered pipeline stage names. Matches the orchestrator's
#: AGENTS.md "Pipeline shape" section verbatim, so a change here
#: must also land in AGENTS.md.
PIPELINE_STAGES: tuple[str, ...] = (
    "scenario",
    "audio",
    "visual",
    "production",
    "assembly",
)


#: Approval gates the orchestrator pauses on. Mirrors
#: :data:`server.strands_agents.pipeline.INTERRUPT_TOOL_NAMES`.
APPROVAL_GATES: tuple[str, ...] = (
    "launch_visual_production",
    "launch_assembly",
    "request_human_approval",
)


@dataclass(frozen=True)
class TranslatedEvent:
    """One playground-bound event produced by the translator.

    The triple (``kind``, ``summary``, ``detail``) matches the
    :meth:`RunStream.emit` signature so callers can forward
    directly without reshaping. A frozen dataclass is used instead
    of a bare tuple so downstream pattern-matching on ``.kind`` /
    ``.detail`` reads clearly and the contract is one type, not
    three positional arguments.
    """

    kind: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


def _stage_kind(stage: str, suffix: str) -> str:
    """Format a per-stage event kind, defending against empty stages."""
    return f"pipeline.stage.{stage or 'unknown'}.{suffix}"


def _tool_kind(tool: str, suffix: str) -> str:
    """Format a per-tool event kind.

    Tool names come straight from the orchestrator's tool registry —
    they're already stable identifiers (``launch_audio_render``,
    ``evaluate_timing``, …). An empty tool name falls back to
    ``unknown`` rather than producing an invalid kind like
    ``pipeline.tool..start``.
    """
    return f"pipeline.tool.{tool or 'unknown'}.{suffix}"


def translate_pipeline_event(
    event_type: str,
    data: dict[str, Any],
) -> TranslatedEvent:
    """Translate one orchestrator event onto the playground event bus.

    Pure function: no I/O, no global state. Given the orchestrator's
    ``(event_type, data)`` it returns the playground's
    ``(kind, summary, detail)`` bundle. Adapter callers emit the
    result onto a :class:`RunStream` so the UI consumes events via
    the same SSE surface c01..c15 already uses.

    Contract:

    * Known ``event_type`` values map to a stable ``kind`` in the
      ``pipeline.*`` namespace. See the module docstring for the
      full table.
    * Missing fields in ``data`` use conservative defaults (``"unknown"``,
      ``0``, ``None``) — the adapter never raises ``KeyError`` on a
      real orchestrator event, because a missing field in the wire
      should surface as "unknown" in the UI, not take down the run.
    * Unknown ``event_type`` values translate to
      ``pipeline.unknown`` with the original ``event_type`` under
      ``detail["source_event_type"]``. This is the "never drop"
      invariant: the UI can show "we saw something we didn't
      recognise" rather than silently losing a frame.

    Args:
        event_type: Orchestrator event type (e.g.
            ``pipeline.stage_started``). Case-sensitive — the
            orchestrator is the source of truth for casing.
        data: Event payload. The translator reads whichever fields
            each event type declares; it does not mutate ``data``.

    Returns:
        One :class:`TranslatedEvent` ready to feed into
        :meth:`RunStream.emit`.
    """

    data = dict(data or {})

    if event_type == "pipeline.run_started":
        topic = str(data.get("topic") or "unknown topic")
        duration = int(data.get("target_duration_sec") or 0)
        language = str(data.get("language") or "en")
        return TranslatedEvent(
            kind="pipeline.run_started",
            summary=(
                f"pipeline run started: topic={topic!r} "
                f"duration={duration}s language={language}"
            ),
            detail={
                "topic": topic,
                "target_duration_sec": duration,
                "language": language,
            },
        )

    if event_type == "pipeline.stage_started":
        stage = str(data.get("stage") or "unknown")
        scene_count = int(data.get("scene_count") or 0)
        return TranslatedEvent(
            kind=_stage_kind(stage, "start"),
            summary=f"stage {stage} started",
            detail={
                "stage": stage,
                "scene_count": scene_count,
            },
        )

    if event_type == "pipeline.stage_finished":
        stage = str(data.get("stage") or "unknown")
        elapsed_ms = int(data.get("elapsed_ms") or 0)
        return TranslatedEvent(
            kind=_stage_kind(stage, "end"),
            summary=f"stage {stage} finished in {elapsed_ms}ms",
            detail={
                "stage": stage,
                "elapsed_ms": elapsed_ms,
            },
        )

    if event_type == "pipeline.stage_failed":
        stage = str(data.get("stage") or "unknown")
        reason = str(data.get("reason") or "unknown")
        inner_detail = data.get("detail") or {}
        return TranslatedEvent(
            kind="pipeline.stage_failed",
            summary=f"stage {stage} failed: {reason}",
            detail={
                "stage": stage,
                "reason": reason,
                "detail": inner_detail,
            },
        )

    if event_type == "pipeline.tool_call_started":
        tool = str(data.get("tool") or "unknown")
        agent = str(data.get("agent") or "unknown")
        args_summary = str(data.get("args_summary") or "")
        return TranslatedEvent(
            kind=_tool_kind(tool, "start"),
            summary=f"{agent} calling {tool}",
            detail={
                "tool": tool,
                "agent": agent,
                "args_summary": args_summary,
            },
        )

    if event_type == "pipeline.tool_call_finished":
        tool = str(data.get("tool") or "unknown")
        agent = str(data.get("agent") or "unknown")
        elapsed_ms = int(data.get("elapsed_ms") or 0)
        ok = bool(data.get("ok", True))
        detail: dict[str, Any] = {
            "tool": tool,
            "agent": agent,
            "elapsed_ms": elapsed_ms,
            "ok": ok,
        }
        envelope = data.get("envelope")
        if isinstance(envelope, dict) and envelope:
            detail["envelope"] = dict(envelope)
        error_class = data.get("error_class")
        if error_class:
            detail["error_class"] = str(error_class)
        error = data.get("error")
        if error:
            detail["error"] = str(error)
        return TranslatedEvent(
            kind=_tool_kind(tool, "end"),
            summary=(
                f"{agent} finished {tool} in {elapsed_ms}ms{'' if ok else ' (failed)'}"
            ),
            detail=detail,
        )

    if event_type == "pipeline.approval_gate":
        gate = str(data.get("gate_name") or "unknown")
        allowed = list(data.get("allowed_decisions") or [])
        detail: dict[str, Any] = {
            "gate_name": gate,
            "allowed_decisions": allowed,
        }
        # Slice 9i: thread the resume coordinates onto the wire so
        # the UI can post the operator's decision to
        # ``POST /playground/approval/resume/{run_id}/{interrupt_id}``
        # without cross-referencing other events.
        interrupt_id = data.get("interrupt_id")
        if isinstance(interrupt_id, str) and interrupt_id:
            detail["interrupt_id"] = interrupt_id
        run_id = data.get("run_id")
        if isinstance(run_id, str) and run_id:
            detail["run_id"] = run_id
        args = data.get("args")
        if isinstance(args, dict):
            detail["args"] = args
        return TranslatedEvent(
            kind="pipeline.approval.waiting",
            summary=f"approval gate {gate} — waiting for human",
            detail=detail,
        )

    if event_type == "pipeline.approval_resumed":
        gate = str(data.get("gate_name") or "unknown")
        decision = str(data.get("decision") or "unknown")
        detail = {
            "gate_name": gate,
            "decision": decision,
        }
        interrupt_id = data.get("interrupt_id")
        if isinstance(interrupt_id, str) and interrupt_id:
            detail["interrupt_id"] = interrupt_id
        run_id = data.get("run_id")
        if isinstance(run_id, str) and run_id:
            detail["run_id"] = run_id
        return TranslatedEvent(
            kind="pipeline.approval.resumed",
            summary=f"approval gate {gate} resumed: {decision}",
            detail=detail,
        )

    if event_type == "pipeline.artifact":
        kind = str(data.get("kind") or "unknown")
        revision_tag = str(data.get("revision_tag") or "")
        scene_num = data.get("scene_num")
        b2_url = data.get("b2_url")
        return TranslatedEvent(
            kind="pipeline.artifact",
            summary=(
                f"artifact {kind} r={revision_tag or '?'}"
                + (f" scene={scene_num}" if scene_num is not None else "")
            ),
            detail={
                "artifact_kind": kind,
                "revision_tag": revision_tag,
                "scene_num": scene_num,
                "b2_url": b2_url,
            },
        )

    if event_type == "pipeline.run_finished":
        status = str(data.get("status") or "unknown")
        final_url = data.get("final_mp4_b2_url")
        return TranslatedEvent(
            kind="pipeline.run_finished",
            summary=f"pipeline run finished: status={status}",
            detail={
                "status": status,
                "final_mp4_b2_url": final_url,
            },
        )

    # "Never drop" clause. An orchestrator event the adapter has
    # not been taught about still lands on the stream so the UI can
    # show it. Better to surface noise than to hide a new event
    # behind a silent filter.
    return TranslatedEvent(
        kind="pipeline.unknown",
        summary=f"unrecognised orchestrator event: {event_type}",
        detail={
            "source_event_type": event_type,
            "source_data": data,
        },
    )


# ── Deterministic simulated pipeline ─────────────────────────────────


@dataclass(frozen=True)
class SimulatedStage:
    """Declarative description of one stage for the simulator.

    Each stage contributes a predictable sequence of events: a
    ``stage_started``, one tool-call pair per item in ``tools``, one
    artifact per item in ``artifacts``, then a ``stage_finished``.
    When ``approval_gate`` is set the simulator also emits an
    ``approval_gate`` event immediately after the stage finishes and
    an ``approval_resumed`` event to unblock. The precise shapes are
    chosen to match the real orchestrator's vocabulary so the
    translator's contract is exercised against realistic wire data.
    """

    name: str
    scene_count: int
    tools: tuple[str, ...]
    artifacts: tuple[tuple[str, str | None], ...]
    elapsed_ms: int
    approval_gate: str | None = None


def default_simulation_stages() -> tuple[SimulatedStage, ...]:
    """Return the five-stage happy-path simulation plan.

    Used by the playground ``/playground/pipeline/runs`` endpoint
    to drive the wire before slice 9 attaches the real
    deepagents-backed orchestrator. The exact stage list and tool
    names match :data:`PIPELINE_STAGES` and the orchestrator's
    default tool registry.
    """

    return (
        SimulatedStage(
            name="scenario",
            scene_count=4,
            tools=("generate_scenario", "evaluate_scenario"),
            artifacts=(("scene_json", "r0001"),),
            elapsed_ms=850,
        ),
        SimulatedStage(
            name="audio",
            scene_count=4,
            tools=(
                "launch_audio_render",
                "await_tasks",
                "evaluate_timing",
            ),
            artifacts=(("audio_wav", "r0001"),),
            elapsed_ms=3200,
        ),
        SimulatedStage(
            name="visual",
            scene_count=4,
            tools=("content_analyst", "visual_concepter"),
            artifacts=(("visual_plan_json", "r0001"),),
            elapsed_ms=1100,
            approval_gate="launch_visual_production",
        ),
        SimulatedStage(
            name="production",
            scene_count=4,
            tools=("launch_visual_production", "await_tasks"),
            artifacts=(("video_mp4", "r0001"),),
            elapsed_ms=6400,
        ),
        SimulatedStage(
            name="assembly",
            scene_count=1,
            tools=("launch_assembly", "launch_b2_sync"),
            artifacts=(
                ("otio_xml", "r0001"),
                ("master_mp4", "r0001"),
            ),
            elapsed_ms=2200,
            approval_gate="launch_assembly",
        ),
    )


def generate_simulation_events(
    *,
    topic: str,
    target_duration_sec: int,
    language: str,
    stages: tuple[SimulatedStage, ...] = (),
) -> list[tuple[str, dict[str, Any]]]:
    """Produce the full canned event sequence for one happy-path run.

    Returns a flat list of ``(event_type, data)`` pairs in wall-clock
    order. Intended for two consumers:

    1. :func:`replay_events_onto_stream`, which feeds it onto a
       :class:`RunStream` with realistic per-step spacing for
       evaluator fixtures.
    2. The strands-evals experiment (``infra_pipeline_adapter``),
       which runs the sequence through :func:`translate_pipeline_event`
       and scores the emitted playground events.

    Args:
        topic: User-supplied documentary topic.
        target_duration_sec: Target final video length.
        language: BCP-47 language code.
        stages: Simulation plan. Defaults to
            :func:`default_simulation_stages` when empty so callers
            can call ``generate_simulation_events(topic=…, …)``
            without juggling plans.

    Returns:
        Flat list of ``(event_type, data)`` pairs.
    """

    if not stages:
        stages = default_simulation_stages()

    events: list[tuple[str, dict[str, Any]]] = []

    events.append(
        (
            "pipeline.run_started",
            {
                "topic": topic,
                "target_duration_sec": target_duration_sec,
                "language": language,
            },
        )
    )

    for stage in stages:
        events.append(
            (
                "pipeline.stage_started",
                {
                    "stage": stage.name,
                    "scene_count": stage.scene_count,
                },
            )
        )
        for tool in stage.tools:
            events.append(
                (
                    "pipeline.tool_call_started",
                    {
                        "tool": tool,
                        "agent": f"{stage.name}_agent",
                        "args_summary": f"stage={stage.name}",
                    },
                )
            )
            events.append(
                (
                    "pipeline.tool_call_finished",
                    {
                        "tool": tool,
                        "agent": f"{stage.name}_agent",
                        "elapsed_ms": max(
                            10, stage.elapsed_ms // max(1, len(stage.tools))
                        ),
                        "ok": True,
                    },
                )
            )
        for kind, revision_tag in stage.artifacts:
            events.append(
                (
                    "pipeline.artifact",
                    {
                        "kind": kind,
                        "revision_tag": revision_tag,
                        "b2_url": (
                            f"b2://documentary/{topic.replace(' ', '_')}"
                            f"/{kind}/{revision_tag}.bin"
                        ),
                    },
                )
            )
        events.append(
            (
                "pipeline.stage_finished",
                {
                    "stage": stage.name,
                    "elapsed_ms": stage.elapsed_ms,
                },
            )
        )
        if stage.approval_gate is not None:
            events.append(
                (
                    "pipeline.approval_gate",
                    {
                        "gate_name": stage.approval_gate,
                        "allowed_decisions": [
                            "accept",
                            "edit",
                            "reject",
                            "respond",
                        ],
                    },
                )
            )
            events.append(
                (
                    "pipeline.approval_resumed",
                    {
                        "gate_name": stage.approval_gate,
                        "decision": "accept",
                    },
                )
            )

    events.append(
        (
            "pipeline.run_finished",
            {
                "status": "ok",
                "final_mp4_b2_url": (
                    f"b2://documentary/{topic.replace(' ', '_')}/master_mp4/r0001.mp4"
                ),
            },
        )
    )

    return events


async def replay_events_onto_stream(
    events: list[tuple[str, dict[str, Any]]],
    stream: Any,
    *,
    per_event_delay_s: float = 0.0,
) -> None:
    """Replay a canned event sequence onto a live ``RunStream``.

    Each ``(event_type, data)`` pair runs through
    :func:`translate_pipeline_event` and the resulting
    :class:`TranslatedEvent` is emitted on the stream via
    ``await stream.emit(kind, summary, detail)``.

    Args:
        events: Sequence produced by
            :func:`generate_simulation_events` (or an equivalent
            source — any list of ``(event_type, data)`` pairs will
            work).
        stream: Anything with an async ``emit(kind, summary, detail)``
            method. The real :class:`RunStream` qualifies; tests
            use a lightweight capture object.
        per_event_delay_s: Optional sleep between events so the UI
            sees a progress-shaped timeline, not a wall of events
            dumped in one frame. Default ``0.0`` keeps the translator
            easy to test — the FastAPI surface passes a small
            positive value.
    """

    for event_type, data in events:
        translated = translate_pipeline_event(event_type, data)
        await stream.emit(
            kind=translated.kind,
            summary=translated.summary,
            detail=translated.detail,
        )
        if per_event_delay_s > 0:
            await asyncio.sleep(per_event_delay_s)


__all__ = [
    "APPROVAL_GATES",
    "PIPELINE_STAGES",
    "SimulatedStage",
    "TranslatedEvent",
    "default_simulation_stages",
    "generate_simulation_events",
    "replay_events_onto_stream",
    "translate_pipeline_event",
]
