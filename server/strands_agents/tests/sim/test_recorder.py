"""Direct-proof tests for :class:`Recorder`."""

from __future__ import annotations

from strands_agents.sim.recorder import CallRecord, Recorder


class TestRecorderBasics:
    def test_records_preserves_order(self) -> None:
        r = Recorder()
        r.record(CallRecord(channel="llm", op="generate_scenario"))
        r.record(CallRecord(channel="tts", op="tts_generate"))
        r.record(CallRecord(channel="renderer", op="dispatch"))
        assert [rec.op for rec in r.records] == [
            "generate_scenario",
            "tts_generate",
            "dispatch",
        ]

    def test_records_is_a_copy(self) -> None:
        r = Recorder()
        r.record(CallRecord(channel="llm", op="generate_scenario"))
        snapshot = r.records
        r.record(CallRecord(channel="llm", op="refine_scenario"))
        # Mutating the recorder after snapshotting must not mutate the
        # snapshot — otherwise trajectory assertions race with the
        # worker threads driving the pool.
        assert [rec.op for rec in snapshot] == ["generate_scenario"]

    def test_ops_filters_by_channel(self) -> None:
        r = Recorder()
        r.record(CallRecord(channel="llm", op="a"))
        r.record(CallRecord(channel="tts", op="b"))
        r.record(CallRecord(channel="llm", op="c"))
        assert r.ops(channel="llm") == ["a", "c"]
        assert r.ops(channel="tts") == ["b"]
        assert r.ops() == ["a", "b", "c"]

    def test_count_matches_channel_and_op(self) -> None:
        r = Recorder()
        r.record(CallRecord(channel="llm", op="x"))
        r.record(CallRecord(channel="llm", op="x"))
        r.record(CallRecord(channel="llm", op="y"))
        r.record(CallRecord(channel="tts", op="x"))
        assert r.count("llm", "x") == 2
        assert r.count("llm", "y") == 1
        assert r.count("tts", "x") == 1
        assert r.count("tts", "y") == 0

    def test_clear_wipes(self) -> None:
        r = Recorder()
        r.record(CallRecord(channel="llm", op="x"))
        r.clear()
        assert r.records == []
        assert r.count("llm", "x") == 0
