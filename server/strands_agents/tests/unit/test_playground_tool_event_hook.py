"""Unit tests for the inner-loop tool-event instrumentation.

Two concerns under test, both introduced to close the
"narration repeats for 200s on long c01 runs" gap.

1. :class:`PlaygroundToolEventEmitter` — the Strands hook that pipes
   every ``@tool`` invocation into the playground event bus. The
   test synthesises the ``BeforeToolCallEvent`` / ``AfterToolCallEvent``
   payloads Strands would fire and asserts the hook emits a
   ``tool.called`` / ``tool.returned`` pair with a step counter,
   latency, input digest, and — when the tool returned scenes — a
   ``num_scenes`` / ``total_duration_sec`` shape.

2. :func:`_tail_to_prompt` + :func:`_context_header` — the narrator's
   prompt builder. The test asserts the context header carries the
   aggregate facts (``total_elapsed``, ``tail_kinds`` histogram,
   dominant kind × count) and that each event line carries every
   rich detail key, not just ``model_id`` + ``elapsed_ms``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from strands_agents.playground.events import Event, RunStream
from strands_agents.playground.narrator import _context_header, _tail_to_prompt
from strands_agents.playground.tool_event_hook import (
    PlaygroundToolEventEmitter,
)


class _FakeRegistry:
    """Minimal ``HookRegistry`` double that records the callbacks registered."""

    def __init__(self) -> None:
        self.callbacks: dict[type, list[Any]] = {}

    def add_callback(self, event_cls: type, callback: Any) -> None:
        self.callbacks.setdefault(event_cls, []).append(callback)


def test_hook_registers_before_and_after_tool_call_callbacks() -> None:
    stream = RunStream("run_hook_reg", component_id="c01", case_name=None)
    hook = PlaygroundToolEventEmitter(stream)
    registry = _FakeRegistry()
    hook.register_hooks(registry)
    # Both Strands lifecycle events are subscribed — tool.called +
    # tool.returned must land in pairs.
    from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent

    assert BeforeToolCallEvent in registry.callbacks
    assert AfterToolCallEvent in registry.callbacks


@pytest.mark.asyncio
async def test_hook_emits_called_then_returned_with_step_counter_and_shape() -> None:
    stream = RunStream("run_hook_emit", component_id="c01", case_name=None)
    stream.attach_loop(asyncio.get_running_loop())
    hook = PlaygroundToolEventEmitter(stream)

    def _fire_hook() -> None:
        # ``emit_sync`` uses ``run_coroutine_threadsafe`` + ``result()``
        # — that would deadlock if called from the same thread as the
        # loop it's posting onto. Run the hook in a worker thread the
        # same way the real playground does (``asyncio.to_thread``).
        hook._on_before(  # type: ignore[arg-type]
            SimpleNamespace(
                tool_use={
                    "name": "generate_scenario",
                    "toolUseId": "use_1",
                    "input": {"topic": "econ", "num_scenes": 6},
                }
            )
        )
        hook._on_after(  # type: ignore[arg-type]
            SimpleNamespace(
                tool_use={
                    "name": "generate_scenario",
                    "toolUseId": "use_1",
                },
                exception=None,
                result={
                    "content": [
                        {
                            "json": {
                                "scenes": [
                                    {"duration_sec": 30.0},
                                    {"duration_sec": 45.0},
                                ]
                            }
                        }
                    ]
                },
            )
        )

    await asyncio.to_thread(_fire_hook)

    events = stream.snapshot()
    kinds = [e.kind for e in events]
    assert kinds == ["tool.called", "tool.returned"]

    called = events[0]
    assert called.detail is not None
    assert called.detail["tool"] == "generate_scenario"
    assert called.detail["step"] == 1
    assert sorted(called.detail["input_keys"]) == ["num_scenes", "topic"]

    returned = events[1]
    assert returned.detail is not None
    assert returned.detail["tool"] == "generate_scenario"
    assert "elapsed_ms" in returned.detail
    assert returned.detail["result_shape"]["num_scenes"] == 2
    assert returned.detail["result_shape"]["total_duration_sec"] == 75.0


@pytest.mark.asyncio
async def test_hook_emits_error_class_on_tool_exception() -> None:
    stream = RunStream("run_hook_err", component_id="c01", case_name=None)
    stream.attach_loop(asyncio.get_running_loop())
    hook = PlaygroundToolEventEmitter(stream)

    def _fire_hook() -> None:
        hook._on_before(  # type: ignore[arg-type]
            SimpleNamespace(
                tool_use={
                    "name": "refine_scenario",
                    "toolUseId": "u2",
                    "input": {},
                }
            )
        )
        hook._on_after(  # type: ignore[arg-type]
            SimpleNamespace(
                tool_use={"name": "refine_scenario", "toolUseId": "u2"},
                exception=ValueError("bad scene"),
                result=None,
            )
        )

    await asyncio.to_thread(_fire_hook)

    events = stream.snapshot()
    returned = events[-1]
    assert returned.detail is not None
    assert returned.detail["error_class"] == "ValueError"
    assert "bad scene" in returned.detail["error"]


def test_context_header_summarises_total_elapsed_and_dominant_kind() -> None:
    events = [
        Event(seq=1, ts=1000.0, kind="run.dispatched", summary="d", detail=None),
        Event(seq=2, ts=1001.0, kind="tool.called", summary="a", detail=None),
        Event(seq=3, ts=1002.0, kind="tool.called", summary="b", detail=None),
        Event(seq=4, ts=1003.0, kind="tool.called", summary="c", detail=None),
        Event(seq=5, ts=1004.0, kind="narrate", summary="n", detail=None),
    ]
    header = _context_header(events, events[-3:], reference=1010.0)
    assert "total_elapsed=10.0s" in header
    assert "total_events=5" in header
    # Tail histogram counts the last 3 events; dominant is tool.called × 2.
    assert "tail_kinds=narrate=1,tool.called=2" in header
    assert "dominant=tool.called×2" in header


def test_tail_to_prompt_surfaces_every_rich_detail_key() -> None:
    events = [
        Event(
            seq=1,
            ts=1000.0,
            kind="tool.called",
            summary="tool.called evaluate_scenario (step 3)",
            detail={
                "tool": "evaluate_scenario",
                "step": 3,
                "input_keys": ["scenes", "target_duration_sec"],
                "input_digest": "{topic: econ, num_scenes: 6}",
            },
        ),
        Event(
            seq=2,
            ts=1001.0,
            kind="tool.returned",
            summary="tool.returned evaluate_scenario in 12030ms",
            detail={
                "tool": "evaluate_scenario",
                "elapsed_ms": 12030,
                "result_shape": {"num_scenes": 6, "rating": "FAIR"},
                "rating": "FAIR",
                "num_issues": 2,
            },
        ),
    ]
    text = _tail_to_prompt(events, now=1005.0)
    # Context header present.
    assert "CONTEXT:" in text
    # Every rich detail key is surfaced on the right line.
    assert "tool=evaluate_scenario" in text
    assert "step=3" in text
    assert "elapsed_ms=12030" in text
    assert "rating=FAIR" in text
    assert "num_issues=2" in text
    assert "input_keys=scenes,target_duration_sec" in text
