"""Scripted approval-gate fake.

Component 15 wraps a handful of tools in ``interrupt_on`` so the
graph pauses and waits for a human decision. In the simulator we
can't wait — the test needs to be deterministic and finish in
milliseconds. :class:`FakeInterrupt` replaces the operator with a
pre-scripted queue of decisions.

Each interrupt-wrapped tool has a canonical name. When the graph
invokes one and pauses, the test harness reads the next decision for
that tool from the queue and resumes the graph with it. If the queue
is empty, :class:`NoScriptedDecision` is raised so missing scripts
never silently accept.

The fake here is passive: it holds the queue and hands out the next
decision when asked. Wiring from the LangGraph ``interrupt`` resume
protocol to this queue lives in the Substrate layer so this module
stays a pure data structure.
"""

from __future__ import annotations

import copy
import threading
from collections import deque
from typing import Any

from strands_agents.sim.recorder import CallRecord, Recorder


class NoScriptedDecision(RuntimeError):
    """Raised when an approval gate is hit with no scripted response."""


class FakeInterrupt:
    """A FIFO of scripted operator decisions keyed by tool name.

    Example:
        >>> fi = FakeInterrupt()
        >>> fi.script(tool_name="launch_visual_production", decision={"type": "accept"})
        >>> fi.script(tool_name="launch_visual_production", decision={
        ...     "type": "edit", "args": {"seed": 42}
        ... })
        >>> fi.next_decision("launch_visual_production")
        {'type': 'accept'}
        >>> fi.next_decision("launch_visual_production")
        {'type': 'edit', 'args': {'seed': 42}}
    """

    def __init__(self, *, recorder: Recorder | None = None) -> None:
        self._lock = threading.Lock()
        self._queues: dict[str, deque[dict[str, Any]]] = {}
        self._recorder = recorder

    def script(self, *, tool_name: str, decision: dict[str, Any]) -> FakeInterrupt:
        """Append one decision for ``tool_name``.

        Returns the instance so calls can be chained.
        """
        if "type" not in decision:
            msg = f"decision for {tool_name!r} missing 'type': {decision!r}"
            raise ValueError(msg)
        # Deep-copy so callers can’t poison the queued decision by
        # mutating a shared nested object after ``script()`` returns.
        # Matches the same guarantee :meth:`FakeLLM.add_rule` makes for
        # scripted chat responses.
        with self._lock:
            self._queues.setdefault(tool_name, deque()).append(copy.deepcopy(decision))
        return self

    def next_decision(self, tool_name: str) -> dict[str, Any]:
        """Pop the next scripted decision for ``tool_name``.

        Raises:
            NoScriptedDecision: If no decisions are queued for the
                tool. The message lists which tools do have queued
                decisions so debugging a scenario doesn't require
                inspecting the fake directly.
        """
        with self._lock:
            queue = self._queues.get(tool_name)
            if not queue:
                available = {k: len(v) for k, v in self._queues.items() if v}
                msg = (
                    f"no scripted approval decision queued for tool_name={tool_name!r}; "
                    f"queues with pending decisions: {available}"
                )
                raise NoScriptedDecision(msg)
            decision = queue.popleft()
        if self._recorder is not None:
            self._recorder.record(
                CallRecord(
                    channel="interrupt",
                    op=tool_name,
                    kwargs={"decision": decision},
                    result_summary=f"type={decision['type']}",
                )
            )
        return decision

    def pending(self, tool_name: str) -> int:
        """Return how many decisions are still queued for ``tool_name``."""
        with self._lock:
            queue = self._queues.get(tool_name)
            return len(queue) if queue else 0

    def exhausted(self) -> bool:
        """Return ``True`` when every queue has been drained."""
        with self._lock:
            return all(len(q) == 0 for q in self._queues.values())
