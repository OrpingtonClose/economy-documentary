"""Per-run event bus for the Component Playground.

The playground's UI uses a single live status line per run (see
``docs/strands-migration/plans/component-playground.md``, "feedback
surface"). That line is driven by a stream of structured events
emitted by the backend as it advances through the run — probing
reachability, dispatching the task, waiting on the LLM, scoring
evaluators, etc.

This module owns the pieces:

* :class:`Event` — one structured step.
* :class:`RunStream` — per-run ring buffer + asyncio condition for
  listeners (SSE clients + the narrator loop).
* :class:`RunRegistry` — process-wide registry keyed by ``run_id``.

Design decisions:

* **Ring buffer**, not unbounded log. We keep the last
  ``_MAX_EVENTS`` events so a stuck run that emits thousands of
  tool-call events doesn't blow memory. The narrator only looks at
  the tail anyway.
* **asyncio.Condition**, not a queue. Multiple listeners (SSE
  client + narrator) need to see every event, so we use a
  shared buffer + a condition to wake anyone waiting.
* **Monotonic sequence numbers** so SSE clients that reconnect can
  resume from the last seen ``seq`` without dedup logic.
* **Thread-safe** via ``asyncio.Lock`` — all callers must be on the
  same event loop. Backend endpoints run on the FastAPI loop; the
  task adapters (which are synchronous) emit via
  :meth:`RunStream.emit_sync` which hops onto the loop safely.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .agui import agui_envelope

_MAX_EVENTS: int = 256
_MAX_RUNS_KEPT: int = 64


@dataclass(frozen=True)
class Event:
    """One structured step in a run's timeline.

    Attributes:
        seq: Monotonic sequence number within the run, starting at 1.
        ts: Unix-seconds timestamp when the event was emitted.
        kind: Short machine-readable label. Stable vocabulary (so the
            narrator prompt and the frontend can both reason about
            them):

            * ``run.dispatched`` — run is about to start;
            * ``probe.start`` / ``probe.done`` — reachability probe;
            * ``task.pick_model`` — adapter chose a reachable model;
            * ``task.start`` / ``task.done`` — task adapter running;
            * ``tool.called`` / ``tool.returned`` — Strands tool call;
            * ``evaluate.start`` / ``evaluate.scored`` — evaluator;
            * ``narrate`` — the narrator LLM emitted a status line;
            * ``interpret`` — post-run interpretation landed;
            * ``run.ok`` / ``run.error`` / ``run.cancelled`` — terminal.
        summary: Short human-readable line for the raw event feed.
            Kept under ~120 chars.
        detail: Structured payload — model id, error class, latency,
            tool name, etc. Opaque to the bus; consumed by the
            narrator prompt and the frontend's disclosure panel.
    """

    seq: int
    ts: float
    kind: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the event.

        Every envelope carries two discriminators side by side:

        * ``kind`` — the legacy internal vocabulary (``tool.called``,
          ``probe.start``, …). Stable; backs the narrator prompt,
          the stall-budget table, and every existing consumer.
        * ``type`` (+ optional ``step_name`` / ``source`` / ``name``
          / ``cancelled``) — the `AG-UI`_ envelope. Same source of
          truth as the legacy kind, derived via
          :func:`.agui.agui_envelope` so the two can't drift.

        See ``server/strands_agents/playground/agui.py`` for the
        authoritative mapping table.

        .. _AG-UI: https://docs.ag-ui.com
        """
        return {
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "summary": self.summary,
            "detail": self.detail,
            **agui_envelope(self.kind),
        }


class RunStream:
    """Event ring buffer + async fan-out for one run."""

    def __init__(
        self,
        run_id: str,
        *,
        component_id: str,
        case_name: str | None,
        max_events: int = _MAX_EVENTS,
    ) -> None:
        self.run_id = run_id
        self.component_id = component_id
        self.case_name = case_name
        self.created_at = time.time()
        self._events: deque[Event] = deque(maxlen=max_events)
        self._seq = 0
        self._closed = False
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)
        #: Populated once the run reaches a terminal state. Not emitted
        #: as an event — consumed directly by the runs collection
        #: endpoint + the post-run interpreter.
        self.terminal: dict[str, Any] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def snapshot(self) -> list[Event]:
        """Return a copy of the current events without locking async."""
        return list(self._events)

    async def emit(
        self,
        kind: str,
        summary: str,
        detail: dict[str, Any] | None = None,
    ) -> Event:
        async with self._condition:
            self._seq += 1
            event = Event(
                seq=self._seq,
                ts=time.time(),
                kind=kind,
                summary=summary,
                detail=detail or {},
            )
            self._events.append(event)
            self._condition.notify_all()
            return event

    def emit_sync(
        self,
        kind: str,
        summary: str,
        detail: dict[str, Any] | None = None,
    ) -> Event | None:
        """Emit from sync code that has a live asyncio loop.

        Used by the synchronous task adapters: they are dispatched via
        ``asyncio.to_thread`` from the ``/runs`` endpoint, and the
        backing loop is stored on the stream in
        :meth:`attach_loop`. If the adapter runs without an attached
        loop (e.g. in unit tests outside FastAPI), the emission is
        dropped — that is the correct behaviour: the event bus is a
        feedback surface, not a contract.
        """
        loop = getattr(self, "_loop", None)
        if loop is None:
            return None
        future = asyncio.run_coroutine_threadsafe(
            self.emit(kind, summary, detail), loop
        )
        try:
            return future.result(timeout=2.0)
        except Exception:  # noqa: BLE001 — emission must never crash the task
            return None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the loop the sync task adapter should post onto."""
        self._loop = loop

    async def close(self, terminal: dict[str, Any] | None = None) -> None:
        async with self._condition:
            self._closed = True
            if terminal is not None:
                self.terminal = terminal
            self._condition.notify_all()

    async def wait_for_after(self, last_seq: int, timeout: float) -> list[Event]:
        """Return events with ``seq > last_seq``, waking on new ones.

        Blocks up to ``timeout`` seconds. Returns an empty list on
        timeout (not an error — the SSE loop uses this to inject a
        heartbeat). Returns the empty list *immediately* once the
        stream is closed and the caller has seen every event.
        """
        deadline = time.time() + timeout
        async with self._condition:
            while True:
                tail = [e for e in self._events if e.seq > last_seq]
                if tail:
                    return tail
                if self._closed:
                    return []
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                try:
                    await asyncio.wait_for(
                        self._condition.wait(), timeout=remaining
                    )
                except TimeoutError:
                    return []


class RunRegistry:
    """Process-wide registry of recent runs, bounded by LRU eviction."""

    def __init__(self, *, max_runs: int = _MAX_RUNS_KEPT) -> None:
        self._runs: dict[str, RunStream] = {}
        self._order: deque[str] = deque()
        self._max_runs = max_runs

    def new_run(
        self, *, component_id: str, case_name: str | None
    ) -> RunStream:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        stream = RunStream(run_id, component_id=component_id, case_name=case_name)
        self._runs[run_id] = stream
        self._order.append(run_id)
        while len(self._order) > self._max_runs:
            evicted = self._order.popleft()
            self._runs.pop(evicted, None)
        return stream

    def get(self, run_id: str) -> RunStream | None:
        return self._runs.get(run_id)

    def recent(self, limit: int = 16) -> list[RunStream]:
        ids = list(self._order)[-limit:]
        return [self._runs[i] for i in ids if i in self._runs]


_registry: RunRegistry = RunRegistry()


def get_registry() -> RunRegistry:
    return _registry


#: ContextVar holding the stream the currently-executing task adapter
#: is feeding. ``_dispatch_run`` sets this before handing the
#: synchronous adapter to ``asyncio.to_thread`` so task code can
#: discover the active stream (and register tool-call hooks against
#: it) without the adapter signature changing. When no run is active
#: (e.g. pytest importing a task) the var is ``None`` and adapters
#: skip instrumentation entirely.
_ACTIVE_STREAM: contextvars.ContextVar[RunStream | None] = contextvars.ContextVar(
    "playground_active_stream", default=None
)


def set_active_stream(stream: RunStream | None) -> contextvars.Token:
    """Bind ``stream`` as the active playground stream for the current context.

    Returns the reset token the caller must pass to
    :func:`reset_active_stream` once the task adapter returns, so
    nested contexts don't leak onto unrelated runs.
    """
    return _ACTIVE_STREAM.set(stream)


def reset_active_stream(token: contextvars.Token) -> None:
    _ACTIVE_STREAM.reset(token)


def get_active_stream() -> RunStream | None:
    """Return the active playground stream, if one is bound.

    Task adapters call this to decide whether to register playground
    hooks (tool-call emission, etc.). Returns ``None`` outside a run,
    which means ``emit_sync`` wouldn't reach a live loop anyway.
    """
    return _ACTIVE_STREAM.get()


__all__ = [
    "Event",
    "RunRegistry",
    "RunStream",
    "get_active_stream",
    "get_registry",
    "reset_active_stream",
    "set_active_stream",
]
