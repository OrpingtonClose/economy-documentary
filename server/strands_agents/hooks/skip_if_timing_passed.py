"""SkipIfTimingPassed — no-op the refiner once timing budget is satisfied.

Ports the ``_skip_if_timing_passed`` callback from
``server/agents/scenario_refiner.py``. In ADK the callback returns a
dummy :class:`Content` to short-circuit the LLM when
``state["timing_passed"]`` is truthy; the Strands equivalent is a hook
that:

* Attaches on :class:`BeforeInvocationEvent` and records whether the
  current invocation should be skipped (purely for observability).
* Attaches on :class:`BeforeToolCallEvent` and cancels every tool call
  so the refiner cannot mutate the scenes array when timing already
  passed. The LLM is still invoked (Strands has no
  ``cancel_invocation`` primitive), but because every tool call is
  cancelled the refiner is a functional no-op: the agent finishes with
  the scenes unchanged and the orchestrator advances.

The orchestrator (component 14) is expected to bypass the refiner
entirely when timing passes; this hook is the safety net for code
paths that invoke the refiner unconditionally (shadow runs, replays,
direct CLI invocations).
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks import (
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

logger = logging.getLogger(__name__)


def _truthy(value: Any) -> bool:
    """Normalise ``timing_passed`` which may round-trip as a string.

    ADK state serialisation can coerce booleans to strings; honour the
    legacy behaviour so the hook keeps working on replayed state.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


class SkipIfTimingPassed(HookProvider):
    """Cancel every tool call when ``state["timing_passed"]`` is truthy.

    Attributes:
        state_key: Optional top-level key whose value holds the
            pipeline blackboard. When ``None`` (the default) the hook
            reads ``timing_passed`` directly off ``agent.state``.
    """

    _CANCEL_MESSAGE = "skipped: timing already passed"

    def __init__(self, *, state_key: str | None = None) -> None:
        self.state_key = state_key

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        """Wire the before-invocation + before-tool-call callbacks."""
        registry.add_callback(BeforeInvocationEvent, self._on_before_invocation)
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)

    def _extract_timing_passed(self, agent: Any) -> bool:
        raw = agent.state
        dumped = raw.get() if hasattr(raw, "get") else raw
        if not isinstance(dumped, dict):
            return False
        scope = dumped
        if self.state_key is not None:
            nested = dumped.get(self.state_key) or {}
            scope = nested if isinstance(nested, dict) else {}
        return _truthy(scope.get("timing_passed"))

    def _on_before_invocation(self, event: BeforeInvocationEvent) -> None:
        if self._extract_timing_passed(event.agent):
            event.invocation_state.setdefault("skip_refiner", True)
            logger.info(
                "hook=<SkipIfTimingPassed> | refiner invocation will be a no-op "
                "(timing_passed=True)"
            )

    def _on_before_tool(self, event: BeforeToolCallEvent) -> None:
        if self._extract_timing_passed(event.agent):
            tool_name = event.selected_tool.tool_name if event.selected_tool else "<unknown>"
            logger.info(
                "hook=<SkipIfTimingPassed>, tool=<%s> | cancelling tool call",
                tool_name,
            )
            event.cancel_tool = self._CANCEL_MESSAGE
