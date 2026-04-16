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
        self._tool_starts: dict[str, float] = {}
        super().__init__()

    def _get_collector(self) -> Any:
        """Get the active PipelineCollector for the current context."""
        try:
            from dashboard import get_active_collector

            return get_active_collector()
        except Exception:
            return None

    @hook
    def before_invocation(self, event: BeforeInvocationEvent) -> None:
        """Emit invocation-start event to dashboard."""
        collector = self._get_collector()
        if not collector:
            return

        state = event.invocation_state
        phase = state.get("pipeline_phase", "unknown")
        collector.phase_start(phase)

    @hook
    def after_invocation(self, event: AfterInvocationEvent) -> None:
        """Emit invocation-end event to dashboard."""
        collector = self._get_collector()
        if not collector:
            return

        state = event.invocation_state
        phase = state.get("pipeline_phase", "unknown")
        collector.phase_end(phase, status="completed")

    @hook
    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        """Emit tool-start event to dashboard."""
        tool_name = event.tool_use.get("name", "unknown")
        tool_id = event.tool_use.get("toolUseId", "")
        self._tool_starts[tool_id] = time.monotonic()

        collector = self._get_collector()
        if not collector:
            return

        agent = "agent"
        collector.tool_start(
            tool_name=tool_name,
            agent=agent,
            args_summary=str(event.tool_use.get("input", {}))[:200],
        )

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

        result_text = str(event.tool_result) if event.tool_result else ""
        collector.tool_end(
            tool_name=tool_name,
            agent="agent",
            duration=elapsed,
            result_chars=len(result_text),
        )
