"""Call recorder — the trajectory-capture backbone for the simulator.

Every fake in :mod:`strands_agents.sim` writes a :class:`CallRecord` to
a shared :class:`Recorder` on every invocation. Trajectory tests assert
on the resulting list: "LLM scenario generator called once, then
launch_audio_render called N times, then evaluate_timing called once"
and so on.

The recorder is intentionally passive — it never blocks, never raises,
never mutates fake behaviour. It just records.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CallRecord:
    """One recorded fake invocation.

    Attributes:
        channel: Which fake produced the record — one of ``"llm"``,
            ``"tts"``, ``"renderer"``, ``"b2"``, ``"clock"``,
            ``"interrupt"``.
        op: The operation within the channel (e.g. ``"generate_scenario"``
            for LLM, ``"dispatch"`` for renderer, ``"upload"`` for B2,
            ``"now"`` for clock).
        args: Positional arguments as a tuple. Opaque — just captured.
        kwargs: Keyword arguments as a dict. Opaque — just captured.
        result_summary: Short string summarising what the fake
            returned. Used for trajectory assertions that don't want
            to compare full payloads. Example: for a scenario-generate
            call, ``"scenes=5 revision=abc123"``.
        t: Monotonic timestamp from the :class:`FakeClock` at the
            moment of recording, or ``None`` if the clock was not
            plumbed in.
    """

    channel: str
    op: str
    args: tuple[Any, ...] = field(default_factory=tuple)
    kwargs: dict[str, Any] = field(default_factory=dict)
    result_summary: str = ""
    t: float | None = None


class Recorder:
    """Thread-safe FIFO call log."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[CallRecord] = []

    def record(self, record: CallRecord) -> None:
        """Append one record.

        Args:
            record: The :class:`CallRecord` to append.
        """
        with self._lock:
            self._records.append(record)

    @property
    def records(self) -> list[CallRecord]:
        """Return a shallow copy of recorded calls in order.

        Copying protects the caller from concurrent mutations while
        they iterate. The list is cheap to copy even for long runs
        because records are frozen dataclasses.
        """
        with self._lock:
            return list(self._records)

    def ops(self, *, channel: str | None = None) -> list[str]:
        """Return just the ``op`` strings, optionally filtered by channel.

        Convenience for trajectory assertions.
        """
        with self._lock:
            return [
                r.op for r in self._records if channel is None or r.channel == channel
            ]

    def count(self, channel: str, op: str) -> int:
        """Return how many times ``op`` was called on ``channel``."""
        with self._lock:
            return sum(
                1 for r in self._records if r.channel == channel and r.op == op
            )

    def clear(self) -> None:
        """Wipe recorded calls. Mostly useful between scripted scenarios
        inside a single test."""
        with self._lock:
            self._records.clear()
