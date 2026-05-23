"""
Reasoning trace hook — Strands replacement for plugins/reasoning_trace.py.

The original reasoning_trace.py was a BasePlugin subclass with 9 ADK
imports (BaseAgent, CallbackContext, InvocationContext, Event, LlmRequest,
LlmResponse, BasePlugin, BaseTool, ToolContext). It intercepted model
requests, tool calls, and responses to surface reasoning to the
dashboard.

The Strands equivalent is a HookProvider that subscribes to the
corresponding lifecycle events:
  - BeforeModelCallEvent → log model request
  - AfterModelCallEvent → log model response
  - BeforeToolCallEvent → log tool call
  - AfterToolCallEvent → log tool result

All events are forwarded to the existing ``emit_agui_event`` SSE bus
so the dashboard contract is unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from strands.hooks import (
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
)

logger = logging.getLogger(__name__)


class ReasoningTraceHook(HookProvider):
    """Surface reasoning chatter to the frontend via the SSE event bus.

    Replaces the 9-ADK-symbol BasePlugin from plugins/reasoning_trace.py
    with a clean HookProvider that fires on the same lifecycle events.
    """

    def __init__(self, emit_fn=None) -> None:
        """Args:
            emit_fn: Optional callable to emit events. Defaults to
                ``emit_agui_event`` (lazy import to avoid circular deps).
        """
        self._emit_fn = emit_fn

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._emit_fn is None:
            try:
                from agui_events import emit_agui_event
                self._emit_fn = emit_agui_event
            except ImportError:
                logger.debug("emit_agui_event not available, skipping emission")
                return
        self._emit_fn(event_type, data)

    async def on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        """Log model request."""
        self._emit("reasoning_trace", {
            "phase": "model_request",
            "timestamp": time.time(),
        })

    async def on_after_model_call(self, event: AfterModelCallEvent) -> None:
        """Log model response."""
        self._emit("reasoning_trace", {
            "phase": "model_response",
            "timestamp": time.time(),
            "exception": str(event.exception) if event.exception else None,
        })

    async def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        """Log tool call."""
        self._emit("reasoning_trace", {
            "phase": "tool_call",
            "tool_name": getattr(event, 'tool_name', 'unknown') if hasattr(event, 'tool_name') else "unknown",
            "timestamp": time.time(),
        })

    async def on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        """Log tool result."""
        self._emit("reasoning_trace", {
            "phase": "tool_result",
            "tool_name": getattr(event, 'tool_name', 'unknown') if hasattr(event, 'tool_name') else "unknown",
            "timestamp": time.time(),
            "exception": str(event.exception) if hasattr(event, 'exception') and event.exception else None,
        })
