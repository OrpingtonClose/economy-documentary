"""Dashboard plugin -- emit SSE events to the PipelineCollector.

Replaces dashboard integration previously done via ADK middleware.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from strands.hooks.events import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
)
from strands.plugins import Plugin, hook

logger = logging.getLogger(__name__)


class DashboardPlugin(Plugin):
    """Emits lifecycle events to PipelineCollector for the SSE dashboard."""

    name = "dashboard"

    def __init__(self) -> None:
        self._collector = None
        self._tool_starts: dict[str, float] = {}
        super().__init__()

    def _get_collector(self) -> Any:
        """Lazy-load PipelineCollector to avoid import cycles."""
        if self._collector is None:
            try:
                from dashboard.collector import PipelineCollector

                self._collector = PipelineCollector.get_instance()
            except ImportError:
                logger.debug("dashboard collector not available")
        return self._collector

    @hook
    def before_invocation(self, event: BeforeInvocationEvent) -> None:
        """Emit invocation-start event to dashboard."""
        collector = self._get_collector()
        if not collector:
            return

        state = event.invocation_state
        collector.emit({
            "type": "invocation_start",
            "agent": state.get("_current_agent", "unknown"),
            "phase": state.get("_current_phase", "unknown"),
            "timestamp": time.time(),
        })

    @hook
    def after_invocation(self, event: AfterInvocationEvent) -> None:
        """Emit invocation-end event to dashboard."""
        collector = self._get_collector()
        if not collector:
            return

        state = event.invocation_state
        collector.emit({
            "type": "invocation_end",
            "agent": state.get("_current_agent", "unknown"),
            "phase": state.get("_current_phase", "unknown"),
            "timestamp": time.time(),
        })

    @hook
    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        """Emit tool-start event to dashboard."""
        tool_name = event.tool_use.get("name", "unknown")
        tool_id = event.tool_use.get("toolUseId", "")
        self._tool_starts[tool_id] = time.monotonic()

        collector = self._get_collector()
        if not collector:
            return

        collector.emit({
            "type": "tool_start",
            "tool": tool_name,
            "tool_id": tool_id,
            "timestamp": time.time(),
        })

    @hook
    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        """Emit tool-end event to dashboard."""
        tool_name = event.tool_use.get("name", "unknown")
        tool_id = event.tool_use.get("toolUseId", "")
        start = self._tool_starts.pop(tool_id, 0.0)
        elapsed = time.monotonic() - start if start else 0.0

        collector = self._get_collector()
        if not collector:
            return

        collector.emit({
            "type": "tool_end",
            "tool": tool_name,
            "tool_id": tool_id,
            "elapsed_ms": int(elapsed * 1000),
            "timestamp": time.time(),
        })
