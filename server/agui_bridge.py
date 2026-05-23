"""Bridge from the legacy agui_events bus to the AG-UI wire.

The production pipeline (agents, gatekeeper, recovery, callbacks)
emits events through the ``agui_events`` pub/sub bus
(``emit_agui_event`` / ``subscribe_agui_events``). The playground
uses its own ``RunStream`` instead. This module bridges the two:

* On startup, it subscribes to ``agui_events`` and re-emits each
  event as an AG-UI ``CustomEvent`` on the playground's RunStream
  (if a run is active).
* It also provides a FastAPI endpoint that streams ``agui_events``
  directly as AG-UI events, for consumers that don't use the
  per-run RunStream (e.g. the CopilotKit sidebar).

This is the "reconnect" step — the old ``server.py`` used to forward
agui_events into the CopilotKit SSE stream. Now they flow through
the AG-UI protocol instead.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from ag_ui.core import CustomEvent, EventType, RunFinishedEvent, RunStartedEvent
from ag_ui.encoder import EventEncoder

from agui_events import subscribe_agui_events, unsubscribe_agui_events

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Global event stream endpoint
# ---------------------------------------------------------------------------

def add_agui_events_endpoint(app: FastAPI, path: str = "/agui") -> None:
    """Mount a global AG-UI events endpoint that streams agui_events.

    GET /agui/events — subscribes to the agui_events bus and yields
    every event as an AG-UI CustomEvent via SSE. This is the
    CopilotKit-compatible replacement for the old SSE stream in
    server.py.

    The endpoint is long-lived: the client stays connected and
    receives events as they're emitted. Disconnection or a 30-second
    idle timeout (no events) closes the stream.
    """

    @app.get(f"{path}/events")
    async def agui_events_stream(request: Request) -> StreamingResponse:
        accept_header = request.headers.get("accept", "")
        encoder = EventEncoder(accept=accept_header)

        thread_id = str(uuid.uuid4())
        run_id = f"global_{uuid.uuid4().hex[:8]}"

        queue: collections.deque = subscribe_agui_events()

        async def event_generator():
            yield encoder.encode(RunStartedEvent(
                type=EventType.RUN_STARTED,
                thread_id=thread_id,
                run_id=run_id,
            ))

            try:
                idle_since = time.monotonic()
                while True:
                    if await request.is_disconnected():
                        break

                    # Drain the queue
                    had_events = False
                    while queue:
                        event = queue.popleft()
                        event_type = event.get("type", "unknown")
                        event_data = event.get("data", {})
                        yield encoder.encode(CustomEvent(
                            type=EventType.CUSTOM,
                            name=event_type,
                            value=event_data,
                        ))
                        had_events = True

                    if had_events:
                        idle_since = time.monotonic()
                    else:
                        # Check idle timeout (30s)
                        if time.monotonic() - idle_since > 30:
                            break
                        await asyncio.sleep(0.05)

            except Exception as exc:
                logger.exception("AG-UI events stream error")
                yield encoder.encode(CustomEvent(
                    type=EventType.CUSTOM,
                    name="stream.error",
                    value={"message": str(exc)},
                ))
            finally:
                unsubscribe_agui_events(queue)

            yield encoder.encode(RunFinishedEvent(
                type=EventType.RUN_FINISHED,
                thread_id=thread_id,
                run_id=run_id,
            ))

        return StreamingResponse(
            event_generator(),
            media_type=encoder.get_content_type(),
        )
