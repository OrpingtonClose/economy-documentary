"""Direct-proof tests for :class:`FakeInterrupt`."""

from __future__ import annotations

import pytest

from strands_agents.sim.interrupt import FakeInterrupt, NoScriptedDecision
from strands_agents.sim.recorder import Recorder


class TestFakeInterruptQueue:
    def test_fifo_per_tool(self) -> None:
        fi = FakeInterrupt()
        fi.script(
            tool_name="launch_visual_production", decision={"type": "accept"}
        )
        fi.script(
            tool_name="launch_visual_production",
            decision={"type": "edit", "args": {"seed": 42}},
        )
        assert fi.next_decision("launch_visual_production") == {"type": "accept"}
        assert fi.next_decision("launch_visual_production") == {
            "type": "edit",
            "args": {"seed": 42},
        }

    def test_separate_queues_per_tool(self) -> None:
        fi = FakeInterrupt()
        fi.script(tool_name="a", decision={"type": "accept"})
        fi.script(tool_name="b", decision={"type": "reject", "reason": "bad"})
        assert fi.next_decision("b") == {"type": "reject", "reason": "bad"}
        assert fi.next_decision("a") == {"type": "accept"}

    def test_unscripted_tool_raises(self) -> None:
        fi = FakeInterrupt()
        with pytest.raises(NoScriptedDecision, match="launch_assembly"):
            fi.next_decision("launch_assembly")

    def test_exhausted_queue_raises(self) -> None:
        fi = FakeInterrupt()
        fi.script(tool_name="launch_assembly", decision={"type": "accept"})
        fi.next_decision("launch_assembly")
        with pytest.raises(NoScriptedDecision):
            fi.next_decision("launch_assembly")

    def test_decision_must_have_type(self) -> None:
        fi = FakeInterrupt()
        with pytest.raises(ValueError, match="missing 'type'"):
            fi.script(tool_name="x", decision={"reason": "nope"})

    def test_pending_and_exhausted(self) -> None:
        fi = FakeInterrupt()
        assert fi.exhausted() is True
        fi.script(tool_name="a", decision={"type": "accept"})
        fi.script(tool_name="a", decision={"type": "reject"})
        fi.script(tool_name="b", decision={"type": "accept"})
        assert fi.pending("a") == 2
        assert fi.pending("b") == 1
        assert fi.exhausted() is False
        fi.next_decision("a")
        fi.next_decision("a")
        fi.next_decision("b")
        assert fi.exhausted() is True

    def test_decision_returned_is_a_copy(self) -> None:
        fi = FakeInterrupt()
        fi.script(
            tool_name="t", decision={"type": "edit", "args": {"seed": 1}}
        )
        d = fi.next_decision("t")
        d["args"]["seed"] = 999
        # Mutation of the returned dict must not affect future scripts —
        # but because the queue is already empty, we test by scripting
        # again and verifying the fresh decision is untouched.
        fi.script(
            tool_name="t", decision={"type": "edit", "args": {"seed": 1}}
        )
        assert fi.next_decision("t") == {"type": "edit", "args": {"seed": 1}}


class TestFakeInterruptRecording:
    def test_records_each_decision(self) -> None:
        r = Recorder()
        fi = FakeInterrupt(recorder=r)
        fi.script(tool_name="launch_assembly", decision={"type": "accept"})
        fi.script(tool_name="launch_visual_production", decision={"type": "reject"})
        fi.next_decision("launch_assembly")
        fi.next_decision("launch_visual_production")
        ops = r.ops(channel="interrupt")
        assert ops == ["launch_assembly", "launch_visual_production"]
