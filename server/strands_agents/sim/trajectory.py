"""Trajectory extraction from a :class:`SimulationResult`.

The simulator captures two independent streams of information while
running a scenario:

1. :class:`~strands_agents.sim.recorder.CallRecord` entries — every
   time a fake *channel* is poked (TTS, renderer, B2, clock, operator).
   Substrate-level, not tool-level.

2. ``final_state["messages"]`` from the LangGraph run — every
   ``AIMessage`` carries the ``tool_calls`` the LLM emitted on that
   turn.  Tool-level, in chronological order, one entry per distinct
   tool invocation.

Trajectory evaluators like
:class:`~strands_agents.evals.evaluators.timing_loop_trajectory.TimingLoopTrajectoryEvaluator`
work against the second stream in the shape ``{"name": str, "args":
dict}``.  :func:`tool_call_trajectory` walks the messages list and
produces exactly that shape so simulator scenarios can be graded with
the same evaluator production trajectories are graded with.

Parallel tool calls (emitted by one ``AIMessage`` carrying multiple
entries in its ``tool_calls`` attribute) are flattened in the order
they appear on the message — this matches the order a human reads the
trajectory in and is how the evaluator counts them.
"""

from __future__ import annotations

from typing import Any

from strands_agents.sim.orchestrator_simulator import SimulationResult


def tool_call_trajectory(result: SimulationResult) -> list[dict[str, Any]]:
    """Extract the ordered list of tool calls from a simulator run.

    Each emitted tool call is tagged with an ``at_turn`` integer that
    identifies which ``AIMessage`` (LLM turn) produced it.  Calls in
    the same turn share an ``at_turn`` value, which is how evaluators
    like :class:`ParallelLaunchEvaluator` detect parallel fan-out.

    Args:
        result: The :class:`SimulationResult` returned by
            :meth:`OrchestratorSimulator.run`.

    Returns:
        A list of ``{"name": str, "args": dict, "at_turn": int}``
        entries, one per tool call the orchestrator emitted, in
        chronological order.  Returns an empty list if the run never
        produced tool calls.
    """
    messages = result.final_state.get("messages", [])
    trajectory: list[dict[str, Any]] = []
    turn_index = 0
    for message in messages:
        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            continue
        for call in tool_calls:
            # LangChain ``tool_calls`` entries are ``{"name", "args",
            # "id"}`` dicts.  We strip ``id`` because the evaluators
            # only care about the semantic ``(name, args, turn)`` triple.
            trajectory.append(
                {
                    "name": call.get("name", ""),
                    "args": dict(call.get("args") or {}),
                    "at_turn": turn_index,
                }
            )
        turn_index += 1
    return trajectory


__all__ = ["tool_call_trajectory"]
