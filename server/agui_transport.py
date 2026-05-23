"""AG-UI transport layer for the playground.

Bridges the playground's internal RunStream event bus to the AG-UI
wire protocol so CopilotKit and other AG-UI consumers can subscribe
to playground events (component runs, pipeline runs, approvals,
artifacts, errors) without custom SSE parsing.

Two modes:

1. **Agent mode** — wraps a Strands agent via ``StrandsAgent`` and
   ``add_strands_fastapi_endpoint``. This is the standard CopilotKit
   integration for chat-driven agents.

2. **Stream mode** — bridges the playground's ``RunStream`` to AG-UI
   events via ``/agui/runs/{run_id}/events``. This is how the
   playground's component/pipeline runs surface on the AG-UI wire.

The playground uses both: agent mode for the CopilotKit sidebar,
stream mode for the component workbench and pipeline orchestrator.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from ag_ui.core import (
    EventType,
    RunFinishedEvent,
    RunStartedEvent,
    RunErrorEvent,
    CustomEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder

from strands_agents.playground.events import Event, get_registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kind → AG-UI event mapping
# ---------------------------------------------------------------------------

_KIND_MAP: dict[str, str] = {
    "run.dispatched": "RUN_STARTED",
    "run.ok": "RUN_FINISHED",
    "run.error": "RUN_ERROR",
    "run.cancelled": "RUN_FINISHED",
    "step.started": "STEP_STARTED",
    "step.finished": "STEP_FINISHED",
    "tool.called": "TOOL_CALL_START",
    "tool.returned": "TOOL_CALL_END",
    "tool.error": "TOOL_CALL_END",
    "narrate": "TEXT_MESSAGE_CONTENT",
    "pipeline.stage.started": "STEP_STARTED",
    "pipeline.stage.finished": "STEP_FINISHED",
    "pipeline.stage.failed": "STEP_FINISHED",
    "pipeline.approval.waiting": "STEP_STARTED",
    "pipeline.approval.resumed": "STEP_FINISHED",
    "pipeline.artifact": "CUSTOM",
    "pipeline.unknown": "CUSTOM",
}


def _event_to_agui(
    event: Event,
    run_id: str,
    thread_id: str,
) -> list[Any]:
    """Convert one playground Event to AG-UI event objects.

    Returns a list because one playground event may produce multiple
    AG-UI events (e.g. a text message needs start + content + end).
    """
    kind = event.kind
    detail = event.detail
    summary = event.summary
    agui_type = _KIND_MAP.get(kind, "CUSTOM")

    events: list[Any] = []

    if agui_type == "RUN_STARTED":
        events.append(RunStartedEvent(
            type=EventType.RUN_STARTED,
            thread_id=thread_id,
            run_id=run_id,
        ))

    elif agui_type == "RUN_FINISHED":
        events.append(RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=thread_id,
            run_id=run_id,
        ))

    elif agui_type == "RUN_ERROR":
        events.append(RunErrorEvent(
            type=EventType.RUN_ERROR,
            message=summary or detail.get("reason", "unknown error"),
            code=detail.get("code", "RUN_ERROR"),
        ))

    elif agui_type == "STEP_STARTED":
        events.append(StepStartedEvent(
            type=EventType.STEP_STARTED,
            step_name=detail.get("stage", detail.get("step", kind)),
        ))

    elif agui_type == "STEP_FINISHED":
        events.append(StepFinishedEvent(
            type=EventType.STEP_FINISHED,
            step_name=detail.get("stage", detail.get("step", kind)),
        ))

    elif agui_type == "TOOL_CALL_START":
        tool_call_id = detail.get("tool_call_id", str(uuid.uuid4()))
        events.append(ToolCallStartEvent(
            type=EventType.TOOL_CALL_START,
            tool_call_id=tool_call_id,
            tool_call_name=detail.get("tool", kind),
            parent_message_id=str(uuid.uuid4()),
        ))

    elif agui_type == "TOOL_CALL_END":
        tool_call_id = detail.get("tool_call_id", str(uuid.uuid4()))
        if detail.get("args"):
            events.append(ToolCallArgsEvent(
                type=EventType.TOOL_CALL_ARGS,
                tool_call_id=tool_call_id,
                delta=detail.get("args", ""),
            ))  # type: ignore[call-arg]
        events.append(ToolCallEndEvent(
            type=EventType.TOOL_CALL_END,
            tool_call_id=tool_call_id,
        ))

    elif agui_type == "TEXT_MESSAGE_CONTENT":
        msg_id = str(uuid.uuid4())
        events.append(TextMessageStartEvent(
            type=EventType.TEXT_MESSAGE_START,
            message_id=msg_id,
            role="assistant",
        ))
        events.append(TextMessageContentEvent(
            type=EventType.TEXT_MESSAGE_CONTENT,
            message_id=msg_id,
            content=summary or str(detail),  # type: ignore[call-arg]
        ))  # type: ignore[call-arg]
        events.append(TextMessageEndEvent(
            type=EventType.TEXT_MESSAGE_END,
            message_id=msg_id,
        ))

    elif agui_type == "CUSTOM":
        events.append(CustomEvent(
            type=EventType.CUSTOM,
            name=kind,
            value=detail,
        ))

    else:
        events.append(CustomEvent(
            type=EventType.CUSTOM,
            name=kind,
            value={"summary": summary, "detail": detail},
        ))

    return events


# ---------------------------------------------------------------------------
# SSE endpoint for stream mode
# ---------------------------------------------------------------------------

def add_playground_agui_endpoint(app: FastAPI, path: str = "/agui") -> None:
    """Mount the playground AG-UI stream endpoint.

    GET /agui/runs/{run_id}/events — subscribes to the RunStream
    for the given run_id and yields AG-UI events via SSE.

    Uses the RunStream's ``wait_for_after`` for efficient async
    polling with a 5-second timeout per iteration (heartbeat window).
    """

    @app.get(f"{path}/runs/{{run_id}}/events")
    async def agui_run_events(run_id: str, request: Request) -> StreamingResponse:
        """Stream AG-UI events for a playground run."""
        accept_header = request.headers.get("accept", "")
        encoder = EventEncoder(accept=accept_header)

        stream = get_registry().get(run_id)
        if stream is None:
            return StreamingResponse(
                iter([encoder.encode(RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message=f"no stream for run_id={run_id}",
                    code="STREAM_NOT_FOUND",
                ))]),
                media_type=encoder.get_content_type(),
                status_code=404,
            )

        thread_id = str(uuid.uuid4())

        async def event_generator():
            # Emit RUN_STARTED
            yield encoder.encode(RunStartedEvent(
                type=EventType.RUN_STARTED,
                thread_id=thread_id,
                run_id=run_id,
            ))

            try:
                last_seq = 0
                while True:
                    # Check if client disconnected
                    if await request.is_disconnected():
                        break

                    # Wait for new events (5s timeout = heartbeat window)
                    new_events = await stream.wait_for_after(last_seq, timeout=5.0)

                    if new_events:
                        for event in new_events:
                            agui_events = _event_to_agui(event, run_id, thread_id)
                            for agui_event in agui_events:
                                yield encoder.encode(agui_event)
                        last_seq = new_events[-1].seq

                    # If the stream is closed and we've seen all events, stop
                    if stream.closed and not new_events:
                        break

            except Exception as exc:
                logger.exception("AG-UI stream error for run_id=%s", run_id)
                yield encoder.encode(RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message=str(exc),
                    code="STREAM_ERROR",
                ))

            # Always emit RUN_FINISHED
            yield encoder.encode(RunFinishedEvent(
                type=EventType.RUN_FINISHED,
                thread_id=thread_id,
                run_id=run_id,
            ))

        return StreamingResponse(
            event_generator(),
            media_type=encoder.get_content_type(),
        )
