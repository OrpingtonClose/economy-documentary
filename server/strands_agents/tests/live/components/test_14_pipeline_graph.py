"""Hermetic proof of robustness for Component 14 (pipeline-graph).

Clear-cut contracts proved here:

1. The orchestrator builds cleanly off the default tool + SubAgent
   surface without any live model call.
2. Every sensitive tool (``launch_visual_production``,
   ``launch_assembly``, ``request_human_approval``) has an
   ``interrupt_on`` entry with a non-empty, vocabulary-compliant
   ``allowed_decisions`` list.
3. The orchestrator system prompt names every one of the five
   canonical stages by keyword so the LLM has them in context from
   turn zero.
4. The run loop declines interrupts deterministically when no
   operator is attached — a CI smoke run with the fake model
   terminates after exactly one interrupt round instead of hanging.
5. The ``max_interrupt_rounds`` safety cap actually fires on a
   graph that wedges in a permanent interrupt state.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage
from langgraph.types import Command

from strands_agents.pipeline import (
    INTERRUPT_TOOL_NAMES,
    ORCHESTRATOR_PROMPT,
    _build_interrupt_on,
    build_default_subagents,
    build_default_tools,
    build_orchestrator,
)
from strands_agents.run import _auto_reject_interrupt, run_documentary


class _FakeAlwaysInterruptGraph:
    """Stand-in for a CompiledStateGraph that refuses to terminate."""

    def __init__(self) -> None:
        self.invocations: list[Any] = []

    async def ainvoke(self, value: Any) -> dict[str, Any]:
        self.invocations.append(value)
        return {"__interrupt__": [{"value": {"tool_name": "launch_assembly"}}]}


class _FakeCleanGraph:
    """Stand-in that yields one interrupt and then runs clean."""

    def __init__(self) -> None:
        self.invocations: list[Any] = []
        self._rounds_left = 1

    async def ainvoke(self, value: Any) -> dict[str, Any]:
        self.invocations.append(value)
        if self._rounds_left > 0:
            self._rounds_left -= 1
            return {
                "__interrupt__": [{"value": {"tool_name": "launch_visual_production"}}]
            }
        return {"final": True, "messages": []}


# ---------------------------------------------------------------------------
# Default tool + subagent surface
# ---------------------------------------------------------------------------


def test_default_tools_cover_every_canonical_leaf() -> None:
    names = {getattr(t, "name", None) for t in build_default_tools()}
    expected = {
        "generate_scenario",
        "evaluate_scenario",
        "refine_scenario",
        "evaluate_timing",
        "launch_audio_render",
        "launch_visual_production",
        "launch_assembly",
        "launch_b2_sync",
        "check_tasks",
        "await_tasks",
        "request_human_approval",
    }
    missing = expected - names
    assert not missing, f"default tools missing: {missing}"


def test_default_subagents_all_carry_name() -> None:
    for sub in build_default_subagents():
        assert "name" in sub, sub


# ---------------------------------------------------------------------------
# interrupt_on wiring
# ---------------------------------------------------------------------------


def test_every_sensitive_tool_has_an_interrupt_gate() -> None:
    cfg = _build_interrupt_on(INTERRUPT_TOOL_NAMES)
    assert set(cfg.keys()) == set(INTERRUPT_TOOL_NAMES)


def test_every_interrupt_entry_allows_at_least_one_decision() -> None:
    cfg = _build_interrupt_on(INTERRUPT_TOOL_NAMES)
    for name, entry in cfg.items():
        assert isinstance(entry, dict), name
        allowed = entry.get("allowed_decisions") or []
        assert allowed, f"{name} has no allowed_decisions"


def test_every_interrupt_decision_is_in_canonical_vocabulary() -> None:
    canonical = {"accept", "edit", "reject", "respond"}
    cfg = _build_interrupt_on(INTERRUPT_TOOL_NAMES)
    for name, entry in cfg.items():
        allowed = set(entry["allowed_decisions"])  # type: ignore[index]
        assert allowed <= canonical, (
            f"{name} allows decisions outside canonical vocabulary: "
            f"{allowed - canonical}"
        )


def test_launch_assembly_drops_edit_decision() -> None:
    # Component 15 vocabulary: launch_assembly must not be editable —
    # an operator can accept or reject but cannot mutate the timeline
    # at resume-time.
    cfg = _build_interrupt_on(["launch_assembly"])
    allowed = set(cfg["launch_assembly"]["allowed_decisions"])  # type: ignore[index]
    assert "edit" not in allowed


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def test_prompt_names_every_canonical_stage() -> None:
    for keyword in (
        "Scenario",
        "Audio",
        "Visual",
        "Production",
        "Assembly",
    ):
        assert keyword in ORCHESTRATOR_PROMPT, keyword


def test_prompt_warns_against_bypassing_gates() -> None:
    lowered = ORCHESTRATOR_PROMPT.lower()
    assert "bypass" in lowered or "interrupt_on" in lowered, (
        "orchestrator prompt must reference the interrupt gates so the "
        "LLM cannot silently route around them"
    )


def test_prompt_instructs_escalation_delegation() -> None:
    assert "escalation" in ORCHESTRATOR_PROMPT.lower()


# ---------------------------------------------------------------------------
# build_orchestrator smoke
# ---------------------------------------------------------------------------


def test_orchestrator_builds_with_default_tools(tmp_path: Path) -> None:
    # Inject a fake chat model so the build doesn't touch a provider.
    fake_model = FakeMessagesListChatModel(
        responses=[AIMessage(content="pipeline smoke test")]
    )
    agent = build_orchestrator(
        tmp_path,
        model=fake_model,
        tools=build_default_tools(),
        subagents=[],
    )
    assert agent is not None
    assert hasattr(agent, "ainvoke")


# ---------------------------------------------------------------------------
# Run loop: auto-reject terminates, max_rounds fires
# ---------------------------------------------------------------------------


def test_auto_reject_returns_reject_command() -> None:
    command = asyncio.run(
        _auto_reject_interrupt(
            {"__interrupt__": [{"value": {"tool_name": "launch_visual_production"}}]}
        )
    )
    assert isinstance(command, Command)
    assert command.resume["type"] == "reject"  # type: ignore[index]


def test_run_documentary_terminates_after_auto_reject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _FakeCleanGraph()
    monkeypatch.setattr(
        "strands_agents.run.build_documentary_orchestrator",
        lambda run_dir, *, model=None: stub,
    )
    result = asyncio.run(run_documentary("topic: gold standard", tmp_path))
    assert result == {"final": True, "messages": []}
    # Two invocations: initial run + one resume after the interrupt.
    assert len(stub.invocations) == 2


def test_run_documentary_respects_max_interrupt_rounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _FakeAlwaysInterruptGraph()
    monkeypatch.setattr(
        "strands_agents.run.build_documentary_orchestrator",
        lambda run_dir, *, model=None: stub,
    )
    with pytest.raises(RuntimeError, match="interrupt loop exceeded max rounds"):
        asyncio.run(
            run_documentary(
                "topic: hyperinflation",
                tmp_path,
                max_interrupt_rounds=3,
            )
        )
