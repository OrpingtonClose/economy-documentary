"""
SSE bridge — feeds Strands agent events into the existing AG-UI event bus.

The architecture decision (from the 4-council analysis) was to KEEP the
existing SSE event bus (in agui_events.py) rather than replacing it with
Strands' ``stream_async()``. The reason: ``stream_async()`` is 1:1
(not pub/sub) and cannot support multiple dashboard subscribers.

Instead, this bridge:
1. Calls ``graph.stream_async()`` to get a 1:1 event stream
2. Translates each Strands event into an AG-UI event
3. Emits via ``emit_agui_event()`` which fans out to all subscribers

This means the dashboard contract is unchanged — it still subscribes
to ``subscribe_agui_events()`` and receives the same event types.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

from strands.multiagent.graph import Graph

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event translation
# ---------------------------------------------------------------------------


def _translate_node_event(node_id: str, status: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Translate a Strands node event into an AG-UI event payload."""
    return {
        "node_id": node_id,
        "status": status,
        "timestamp": time.time(),
        **(data or {}),
    }


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class SSEBridge:
    """Bridge from Strands stream_async() to the AG-UI SSE event bus.

    Usage::

        bridge = SSEBridge(graph)
        await bridge.run(task="Make a documentary about...")
    """

    def __init__(self, graph: Graph) -> None:
        self.graph = graph

    async def run(self, task: str) -> None:
        """Run the graph and bridge all events to the AG-UI bus."""
        try:
            from agui_events import emit_agui_event
        except ImportError:
            logger.warning("SSEBridge: emit_agui_event not available, events will be logged only")
            emit_agui_event = lambda t, d: logger.debug("SSE event: %s %s", t, d)

        async for event in self.graph.stream_async(task):
            event_type = self._classify_event(event)
            if event_type:
                emit_agui_event(event_type, event)

    @staticmethod
    def _classify_event(event: dict[str, Any]) -> str | None:
        """Classify a Strands stream event into an AG-UI event type."""
        # Strands stream events have a 'type' field
        event_type = event.get("type", "")
        if "node" in event_type:
            return "pipeline_node"
        if "tool" in event_type:
            return "pipeline_tool"
        if "model" in event_type:
            return "pipeline_model"
        if "interrupt" in event_type:
            return "pipeline_interrupt"
        if "complete" in event_type or "result" in event_type:
            return "pipeline_complete"
        if "error" in event_type:
            return "pipeline_error"
        return None


async def bridge_stream(graph: Graph, task: str) -> None:
    """Convenience function: run the SSE bridge."""
    bridge = SSEBridge(graph)
    await bridge.run(task)
