"""Direct-proof tests for :class:`FakeClock`."""

from __future__ import annotations

import pytest

from strands_agents.sim.clock import FakeClock
from strands_agents.sim.recorder import Recorder


class TestFakeClockBasics:
    def test_starts_at_zero_by_default(self) -> None:
        c = FakeClock()
        assert c.now() == 0.0

    def test_honours_start_value(self) -> None:
        c = FakeClock(start=1000.0)
        assert c.now() == 1000.0

    def test_advance_moves_forward(self) -> None:
        c = FakeClock()
        c.advance(5.0)
        assert c.now() == pytest.approx(5.0)
        c.advance(2.5)
        assert c.now() == pytest.approx(7.5)

    def test_negative_advance_raises(self) -> None:
        c = FakeClock()
        with pytest.raises(ValueError, match="negative"):
            c.advance(-0.1)

    def test_sleep_is_advance(self) -> None:
        c = FakeClock()
        c.sleep(3.0)
        assert c.now() == pytest.approx(3.0)

    def test_monotonic_under_zero_advance(self) -> None:
        c = FakeClock()
        c.advance(1.0)
        before = c.now()
        c.advance(0.0)
        after = c.now()
        assert after == before


class TestFakeClockRecording:
    def test_records_every_op(self) -> None:
        r = Recorder()
        c = FakeClock(recorder=r)
        c.now()
        c.advance(2.0)
        c.sleep(1.0)
        c.now()
        ops = r.ops(channel="clock")
        assert ops == ["now", "advance", "advance", "now"]
        # Timestamps must be non-decreasing so trajectory replays are sane.
        ts = [rec.t for rec in r.records if rec.t is not None]
        assert ts == sorted(ts)
