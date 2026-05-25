"""UI-07 — per-run SSE event ring buffer and run-id registry.

This module owns the infrastructure that makes a pipeline run survive a
browser refresh:

* **Stable run id.** Each pipeline run gets a `run-<hex>` id. The id is
  generated in the AG-UI run middleware and exposed via
  ``GET /api/current-run`` (see :mod:`server.server`).
* **Bounded ring buffer.** Every SSE chunk emitted on ``POST /`` for a
  run is appended to a per-run ring buffer keyed by that run id, with a
  monotonic sequence id used as the SSE ``id:`` field. The buffer is
  bounded (default 10 000 entries); when it fills up the oldest entries
  are evicted and we remember the highest evicted seq so clients can
  detect "buffer overflow" on reconnect.
* **Last-Event-ID replay.** On a resume POST, the endpoint consults the
  registry: if the client's ``Last-Event-ID`` sits inside the buffer,
  every event with ``seq > last_id`` is re-emitted verbatim. If the
  client's last id is older than the buffer tail, a special
  ``buffer_overflow`` event is emitted first so the UI can surface a
  "some events missed — snapshot refetched" banner.

The registry is intentionally transport-agnostic: it stores pre-encoded
SSE text (``data: ...\\n\\n``) alongside each seq id. Callers are
responsible for forming the SSE payload before appending.

Thread-safety: all mutating operations take an internal lock so the
registry can be called from any thread (FastAPI request handlers,
pipeline callbacks running on the ADK executor, etc.).
"""

from __future__ import annotations

import asyncio
import collections
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


# Default bound chosen to match UI-07 parent spec (~10k events). Can be
# overridden via env for tests that want to exercise eviction quickly.
_DEFAULT_MAXLEN = 10000


@dataclass
class _RunEntry:
    run_id: str
    seq: int = 0
    # Highest seq id that has already been evicted. Clients whose
    # Last-Event-ID is <= this value have missed events and must
    # re-hydrate from the snapshot.
    evicted_hi: int = 0
    created_at: float = field(default_factory=time.time)
    buffer: "collections.deque[tuple[int, str]]" = field(
        default_factory=lambda: collections.deque(maxlen=_DEFAULT_MAXLEN)
    )
    subscribers: list["asyncio.Queue[tuple[int, str]]"] = field(default_factory=list)


class RunRegistry:
    """Thread-safe registry of active pipeline runs and their event logs."""

    def __init__(self, maxlen: int = _DEFAULT_MAXLEN) -> None:
        self._maxlen = maxlen
        self._runs: "collections.OrderedDict[str, _RunEntry]" = collections.OrderedDict()
        self._latest_run_id: Optional[str] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def new_run_id(self) -> str:
        """Mint a fresh stable run id (``run-<12 hex>``)."""
        return f"run-{uuid.uuid4().hex[:12]}"

    def create(self, run_id: Optional[str] = None) -> str:
        """Register a new run and make it the "latest". Returns the id.

        If ``run_id`` is omitted a new one is generated.
        """
        rid = run_id or self.new_run_id()
        with self._lock:
            if rid not in self._runs:
                self._runs[rid] = _RunEntry(
                    run_id=rid,
                    buffer=collections.deque(maxlen=self._maxlen),
                )
            self._latest_run_id = rid
        return rid

    def exists(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._runs

    def latest(self) -> Optional[str]:
        with self._lock:
            return self._latest_run_id

    # ------------------------------------------------------------------
    # Append / replay
    # ------------------------------------------------------------------

    def append(self, run_id: str, sse_text: str) -> int:
        """Append an SSE text chunk to the run's buffer, returning the seq id.

        The caller must have already formatted ``sse_text`` as a complete
        SSE event (ending with ``\\n\\n``). The registry does **not**
        prepend ``id:`` — that happens at the transport layer in
        :func:`server.server.unified_agui_endpoint`, so replay output
        exactly matches original live output.

        If the run id is unknown, this is a no-op and returns 0. This
        keeps cross-request race conditions (e.g. pipeline callbacks
        emitting after a run's collector was torn down) harmless.
        """
        subscribers_snapshot: list["asyncio.Queue[tuple[int, str]]"] = []
        seq = 0
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return 0
            entry.seq += 1
            seq = entry.seq
            buf = entry.buffer
            if buf.maxlen is not None and len(buf) == buf.maxlen:
                # deque will evict the leftmost on append; record its seq
                # so we can report buffer_overflow on replay.
                evicted_seq, _ = buf[0]
                entry.evicted_hi = max(entry.evicted_hi, evicted_seq)
            buf.append((seq, sse_text))
            subscribers_snapshot = list(entry.subscribers)
        for q in subscribers_snapshot:
            try:
                q.put_nowait((seq, sse_text))
            except asyncio.QueueFull:
                # Slow subscriber — drop; the ring buffer still holds the
                # event so a reconnect would catch up via replay.
                pass
        return seq

    def replay(
        self, run_id: str, last_event_id: int
    ) -> tuple[list[tuple[int, str]], bool, int]:
        """Return events with seq > ``last_event_id`` still in the buffer.

        Returns ``(events, overflow, latest_seq)``:

        * ``events`` — list of ``(seq, sse_text)`` in order.
        * ``overflow`` — True if the client's ``last_event_id`` precedes
          the oldest entry still in the buffer (i.e. we evicted events
          the client has not seen). The client should refetch snapshot.
        * ``latest_seq`` — the current seq id of the run, so the caller
          can report it to the client (debug / diagnostics).
        """
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return [], False, 0
            buf = list(entry.buffer)
            latest = entry.seq
            evicted_hi = entry.evicted_hi
        if not buf:
            # Nothing buffered. Overflow only if the client claims a
            # last-event-id we definitely evicted.
            overflow = last_event_id > 0 and evicted_hi >= last_event_id
            return [], overflow, latest
        oldest_seq = buf[0][0]
        # Overflow when the client is asking for events older than the
        # oldest entry we still have. last_event_id==0 is "fresh tab,
        # give me everything in the buffer" and never overflows.
        overflow = last_event_id > 0 and last_event_id < oldest_seq - 1
        if last_event_id <= 0:
            # Full replay of whatever is still in buffer.
            return list(buf), overflow, latest
        events = [(s, t) for (s, t) in buf if s > last_event_id]
        return events, overflow, latest

    # ------------------------------------------------------------------
    # Live subscription (for resume connections that want to tail after
    # replay catches up). Currently only used by tests — the main server
    # uses replay-only semantics for resume — but the primitive is here
    # so we can extend later without a migration.
    # ------------------------------------------------------------------

    def subscribe(self, run_id: str) -> Optional["asyncio.Queue[tuple[int, str]]"]:
        q: "asyncio.Queue[tuple[int, str]]" = asyncio.Queue(maxsize=1024)
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return None
            entry.subscribers.append(q)
        return q

    def unsubscribe(
        self, run_id: str, queue: "asyncio.Queue[tuple[int, str]]"
    ) -> None:
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return
            try:
                entry.subscribers.remove(queue)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Drop all runs. Tests only."""
        with self._lock:
            self._runs.clear()
            self._latest_run_id = None

    def buffer_size(self, run_id: str) -> int:
        with self._lock:
            entry = self._runs.get(run_id)
            return len(entry.buffer) if entry else 0

    def latest_seq(self, run_id: str) -> int:
        with self._lock:
            entry = self._runs.get(run_id)
            return entry.seq if entry else 0


# Module-level singleton. Tests can call ``get_run_registry().reset()``
# in a fixture to get a clean slate.
_registry: Optional[RunRegistry] = None
_registry_lock = threading.Lock()


def get_run_registry() -> RunRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = RunRegistry()
        return _registry


__all__ = ["RunRegistry", "get_run_registry"]
