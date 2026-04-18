"""
Unit tests for ARCH-B2 consistency-gate wiring (issue #138).

Covers the invariants declared in ``server/callbacks/consistency_gate.py``:

1. **Callback composition preserves original behaviour.** Composed
   ``after_agent_callback`` and ``before_agent_callback`` invoke the
   original callback and return whatever it returned.
2. **A5 consistency check runs at every invocation point.** After-agent,
   before-agent, before-tool, and gate-poll all emit a drift signal when
   the ledger has advanced past the stage's derivation revision.
3. **B3 drift handler runs when drift is detected.** Queued drift
   signals are drained and handled in the same callback hop.
4. **Idempotent agent-tree wiring.** ``wire_consistency_checks_into_agents``
   tags each agent so a second call does not double-chain.
5. **Gate-poll check does not re-gate reconstruction** (would deadlock).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from callbacks.artifact_revision_tag import tag_artifact  # noqa: E402
from callbacks.consistency_checker import (  # noqa: E402
    pending_drift_signals,
    record_stage_derivation,
)
from callbacks.consistency_gate import (  # noqa: E402
    gate_poll_consistency_check,
    make_after_agent_with_consistency,
    make_before_agent_with_consistency,
    make_before_tool_with_consistency,
    wire_consistency_checks_into_agents,
)
from callbacks.preference_ledger import (  # noqa: E402
    PREFERENCE_LEDGER_KEY,
    Origin,
    Polarity,
    Scope,
    Subject,
    append_preference,
)
from callbacks.remanifestation import (  # noqa: E402
    remanifestation_history,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _origin(event_id: str = "L4-001") -> Origin:
    return Origin(
        l4_event_id=event_id,
        reviewer="tester",
        timestamp="2026-04-18T12:00:00Z",
    )


def _seed_ledger(state: dict, n: int = 1, start: int = 1) -> None:
    for i in range(n):
        append_preference(
            state,
            scope=Scope.SCENE,
            scope_ref=f"scene-{start + i}",
            polarity=Polarity.PREFER,
            subject=Subject.TONE,
            content=f"record {start + i}",
            origin=_origin(f"L4-{start + i:03d}"),
        )


def _state_with_drift(stage: str = "scenario") -> dict:
    """State where the ledger has advanced past the stage's derivation."""
    state = {PREFERENCE_LEDGER_KEY: "[]"}
    _seed_ledger(state, 1)
    state[f"{stage}_scenes"] = [{"id": "s1"}]
    tag_artifact(state, f"{stage}_scenes", stage=stage)
    record_stage_derivation(
        state, stage, revision=1, artifact_ids=[f"{stage}_scenes"]
    )
    _seed_ledger(state, 2, start=2)
    return state


def _state_without_drift(stage: str = "scenario") -> dict:
    """State where the stage derivation matches the current ledger."""
    state = {PREFERENCE_LEDGER_KEY: "[]"}
    _seed_ledger(state, 2)
    state[f"{stage}_scenes"] = [{"id": "s1"}]
    tag_artifact(state, f"{stage}_scenes", stage=stage)
    record_stage_derivation(
        state, stage, revision=2, artifact_ids=[f"{stage}_scenes"]
    )
    return state


def _ctx(state: dict, agent_name: str = "scenario") -> SimpleNamespace:
    return SimpleNamespace(state=state, agent_name=agent_name)


class _NullEscalator:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, state, drift, plan, reason):
        self.calls += 1
        return f"ESC-{self.calls}"


# ---------------------------------------------------------------------------
# after_agent composition
# ---------------------------------------------------------------------------


def test_after_agent_preserves_original_return_value_and_invokes_a5():
    state = _state_with_drift()
    seen: list[str] = []

    def original(ctx):
        seen.append(ctx.agent_name)
        return "ORIGINAL"

    cb = make_after_agent_with_consistency(
        original, escalator=_NullEscalator(), gate=False
    )
    out = cb(_ctx(state))

    assert out == "ORIGINAL"
    assert seen == ["scenario"]
    # A5 drift detected AND B3 handled it (queue drained, history entry).
    assert pending_drift_signals(state) == []
    hist = remanifestation_history(state)
    assert hist and hist[0]["status"] in ("executed", "escalated")


def test_after_agent_is_a_no_op_when_no_drift_is_detected():
    state = _state_without_drift()
    cb = make_after_agent_with_consistency(None, gate=False)
    out = cb(_ctx(state))

    assert out is None
    assert pending_drift_signals(state) == []
    assert remanifestation_history(state) == []


def test_after_agent_accepts_none_original_callback():
    state = _state_with_drift()
    cb = make_after_agent_with_consistency(
        None, escalator=_NullEscalator(), gate=False
    )
    assert cb(_ctx(state)) is None


# ---------------------------------------------------------------------------
# before_agent composition
# ---------------------------------------------------------------------------


def test_before_agent_checks_drift_before_running_original():
    state = _state_with_drift()
    order: list[str] = []

    def original(ctx):
        order.append("original")
        # By the time original runs, A5 must have already signalled drift
        # and B3 must have handled it (signals drained).
        assert pending_drift_signals(state) == []
        return None

    cb = make_before_agent_with_consistency(
        original, escalator=_NullEscalator(), gate=False
    )
    cb(_ctx(state))

    assert order == ["original"]
    hist = remanifestation_history(state)
    assert hist  # B3 handler appended


def test_before_agent_propagates_skip_marker_from_original():
    state = _state_without_drift()

    def original(_ctx):
        return "SKIP"

    cb = make_before_agent_with_consistency(original, gate=False)
    assert cb(_ctx(state)) == "SKIP"


# ---------------------------------------------------------------------------
# before_tool composition
# ---------------------------------------------------------------------------


def test_before_tool_runs_consistency_and_then_original():
    state = _state_with_drift()
    seen: list[str] = []

    def original(tool, args, tool_ctx):
        seen.append("original")
        return None

    cb = make_before_tool_with_consistency(
        original, escalator=_NullEscalator(), gate=False
    )
    tool_ctx = SimpleNamespace(state=state, agent_name="scenario")
    cb("fake_tool", {"x": 1}, tool_ctx)

    assert seen == ["original"]
    # Drift was dispatched
    assert pending_drift_signals(state) == []
    assert remanifestation_history(state)


def test_before_tool_surfaces_original_reject_value():
    state = _state_without_drift()

    def original(_tool, _args, _ctx):
        return {"error": "rate_limited"}

    cb = make_before_tool_with_consistency(original, gate=False)
    tool_ctx = SimpleNamespace(state=state, agent_name="scenario")
    out = cb("fake_tool", {}, tool_ctx)
    assert out == {"error": "rate_limited"}


def test_before_tool_with_none_original_is_a_noop_on_clean_state():
    state = _state_without_drift()
    cb = make_before_tool_with_consistency(None, gate=False)
    tool_ctx = SimpleNamespace(state=state, agent_name="scenario")
    assert cb("tool", {}, tool_ctx) is None


# ---------------------------------------------------------------------------
# Gate-poll invocation point
# ---------------------------------------------------------------------------


def test_gate_poll_detects_drift_and_dispatches_b3_without_recursive_gate():
    state = _state_with_drift()
    drift = gate_poll_consistency_check(
        state, "scenario", escalator=_NullEscalator()
    )
    assert drift is not None
    assert drift.stage_name == "scenario"
    # Drift handled in the same hop -- queue drained, history recorded.
    assert pending_drift_signals(state) == []
    hist = remanifestation_history(state)
    assert hist


def test_gate_poll_returns_none_when_no_drift():
    state = _state_without_drift()
    drift = gate_poll_consistency_check(state, "scenario")
    assert drift is None
    assert remanifestation_history(state) == []


def test_gate_poll_propagates_invariant_violation():
    """Missing ledger -> A5 invariant violation -> RuntimeError, per #138
    'no silent degradation' / fail-loud rule."""
    with pytest.raises(RuntimeError, match="preference_ledger"):
        gate_poll_consistency_check({}, "scenario")


# ---------------------------------------------------------------------------
# Agent-tree wiring
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Minimal stand-in for an ADK Agent used by wiring tests."""

    def __init__(self, name: str, sub_agents=None, has_tools: bool = False):
        self.name = name
        self.sub_agents = sub_agents or []
        self.after_agent_callback = None
        self.before_agent_callback = None
        if has_tools:
            self.before_tool_callback = None


def test_wire_consistency_checks_into_agents_composes_every_agent():
    leaf = _FakeAgent("leaf", has_tools=True)
    mid = _FakeAgent("mid", sub_agents=[leaf])
    root = _FakeAgent("root", sub_agents=[mid])

    wired = wire_consistency_checks_into_agents(root, gate=False)

    assert set(wired) == {"root", "mid", "leaf"}
    for agent in (root, mid, leaf):
        assert callable(agent.after_agent_callback)
        assert callable(agent.before_agent_callback)
    assert callable(leaf.before_tool_callback)


def test_wire_is_idempotent_on_second_call():
    leaf = _FakeAgent("leaf", has_tools=True)
    root = _FakeAgent("root", sub_agents=[leaf])

    first = wire_consistency_checks_into_agents(root, gate=False)
    after_first = leaf.after_agent_callback
    second = wire_consistency_checks_into_agents(root, gate=False)

    # First call wired both, second call wired nothing.
    assert set(first) == {"root", "leaf"}
    assert second == []
    # Callbacks were not re-wrapped (identity preserved).
    assert leaf.after_agent_callback is after_first


def test_wire_preserves_existing_callbacks():
    leaf = _FakeAgent("leaf", has_tools=True)

    def orig_after(ctx):
        return "original-after"

    def orig_tool(tool, args, ctx):
        return None

    leaf.after_agent_callback = orig_after
    leaf.before_tool_callback = orig_tool

    state = _state_with_drift()
    wire_consistency_checks_into_agents(
        _FakeAgent("root", sub_agents=[leaf]),
        escalator=_NullEscalator(),
        gate=False,
    )

    # Original after-callback return value still propagated.
    out = leaf.after_agent_callback(_ctx(state))
    assert out == "original-after"
    # And drift was detected + dispatched.
    assert pending_drift_signals(state) == []
