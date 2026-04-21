"""Deterministic clock fake.

Real-world time is banned from simulator runs for three reasons: it
makes tests flaky (timeouts fire at different points run-to-run), it
makes long-running scenarios slow (a 45-minute pipeline takes 45
minutes), and it hides races (concurrency bugs surface only on fast
machines). :class:`FakeClock` replaces every ``time.monotonic()`` /
``time.sleep()`` call the pipeline makes with a driven, monotonic,
test-owned cursor.
"""

from __future__ import annotations

import threading

from strands_agents.sim.recorder import CallRecord, Recorder


class FakeClock:
    """A monotonic clock whose hands only move when a test moves them.

    The clock starts at ``0.0``. Callers either read the current time
    via :meth:`now` or advance it via :meth:`advance`. ``sleep`` is
    implemented as a plain advance so any pipeline code that calls
    ``fake_clock.sleep(5)`` simulates five seconds passing
    instantly.
    """

    def __init__(self, *, recorder: Recorder | None = None, start: float = 0.0) -> None:
        """Create a clock.

        Args:
            recorder: Optional :class:`Recorder` to capture each
                operation. Trajectory tests plumb this in via
                :class:`~strands_agents.sim.substrate.Substrate`.
            start: Initial value of the clock cursor in seconds.
        """
        self._lock = threading.Lock()
        self._t = float(start)
        self._recorder = recorder

    def now(self) -> float:
        """Return the current clock value without advancing it."""
        with self._lock:
            t = self._t
        if self._recorder is not None:
            self._recorder.record(
                CallRecord(channel="clock", op="now", result_summary=f"t={t}", t=t)
            )
        return t

    def advance(self, seconds: float) -> float:
        """Advance the clock by ``seconds`` and return the new value.

        Args:
            seconds: Non-negative number of seconds to advance. A
                negative value raises :class:`ValueError` so
                accidentally subtracting never reorders recorded
                timestamps.
        """
        if seconds < 0:
            msg = f"cannot advance FakeClock by negative amount: {seconds}"
            raise ValueError(msg)
        with self._lock:
            self._t += float(seconds)
            t = self._t
        if self._recorder is not None:
            self._recorder.record(
                CallRecord(
                    channel="clock",
                    op="advance",
                    args=(seconds,),
                    result_summary=f"t={t}",
                    t=t,
                )
            )
        return t

    def sleep(self, seconds: float) -> None:
        """Pretend to sleep. Equivalent to :meth:`advance` returning
        no value. Kept as a separate method so call sites that write
        ``clock.sleep(x)`` rather than ``clock.advance(x)`` still read
        naturally."""
        self.advance(seconds)
