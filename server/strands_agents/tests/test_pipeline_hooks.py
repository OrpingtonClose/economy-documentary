"""Tests for the 8 pipeline hooks."""

from __future__ import annotations

import pytest

from strands_agents.hooks.pipeline_hooks import (
    StageContractHook,
    ImmutabilityHook,
    BudgetHook,
    ApprovalGateHook,
    ScopeHook,
    QANodeHook,
    CheckpointHook,
    ShellGuardHook,
    ALL_PIPELINE_HOOKS,
)


class TestStageContractHook:
    def test_blocks_node_with_missing_preconditions(self):
        hook = StageContractHook(contracts={
            "audio": {"preconditions": ["scenes", "brief"], "postconditions": ["narration"]},
        })
        # Missing both preconditions
        event = _MockBeforeNodeEvent(node_id="audio", invocation_state={})
        import asyncio
        asyncio.get_event_loop().run_until_complete(hook.on_before_node_call(event))
        assert event.cancel_node is True

    def test_allows_node_with_all_preconditions(self):
        hook = StageContractHook(contracts={
            "audio": {"preconditions": ["scenes", "brief"], "postconditions": ["narration"]},
        })
        event = _MockBeforeNodeEvent(
            node_id="audio",
            invocation_state={"scenes": [], "brief": "test"},
        )
        import asyncio
        asyncio.get_event_loop().run_until_complete(hook.on_before_node_call(event))
        assert event.cancel_node is False

    def test_no_contract_means_no_check(self):
        hook = StageContractHook(contracts={})
        event = _MockBeforeNodeEvent(node_id="audio", invocation_state={})
        import asyncio
        asyncio.get_event_loop().run_until_complete(hook.on_before_node_call(event))
        assert event.cancel_node is False


class TestImmutabilityHook:
    def test_blocks_mutation_tools(self):
        hook = ImmutabilityHook()
        for tool_name in ImmutabilityHook.MUTATION_TOOLS:
            event = _MockBeforeToolEvent(tool_name=tool_name)
            import asyncio
            asyncio.get_event_loop().run_until_complete(hook.on_before_tool_call(event))
            assert event._cancelled, f"Should block {tool_name}"

    def test_allows_safe_tools(self):
        hook = ImmutabilityHook()
        event = _MockBeforeToolEvent(tool_name="otio_read")
        import asyncio
        asyncio.get_event_loop().run_until_complete(hook.on_before_tool_call(event))
        assert event._cancelled is False


class TestBudgetHook:
    def test_tracks_accrued_cost(self):
        hook = BudgetHook(budget_usd=10.0)
        assert hook.accrued == 0.0
        assert hook.budget == 10.0

    def test_accumulates_cost(self):
        hook = BudgetHook(budget_usd=10.0)
        event = _MockAfterNodeEvent(invocation_state={"_stage_cost": 3.0})
        import asyncio
        asyncio.get_event_loop().run_until_complete(hook.on_after_node_call(event))
        assert hook.accrued == 3.0


class TestApprovalGateHook:
    def test_interrupts_unapproved_stage(self):
        hook = ApprovalGateHook(gated_stages={"scenario"})
        event = _MockBeforeNodeEvent(node_id="scenario", invocation_state={})
        import asyncio
        asyncio.get_event_loop().run_until_complete(hook.on_before_node_call(event))
        assert event._interrupted

    def test_allows_approved_stage(self):
        hook = ApprovalGateHook(gated_stages={"scenario"})
        event = _MockBeforeNodeEvent(
            node_id="scenario",
            invocation_state={"_approved_scenario": True},
        )
        import asyncio
        asyncio.get_event_loop().run_until_complete(hook.on_before_node_call(event))
        assert event._interrupted is False


class TestShellGuardHook:
    def test_blocks_disallowed_binary(self):
        hook = ShellGuardHook()
        event = _MockBeforeToolEvent(
            tool_name="shell_safe",
            tool_args={"command": "rm -rf /"},
        )
        import asyncio
        asyncio.get_event_loop().run_until_complete(hook.on_before_tool_call(event))
        assert event._cancelled

    def test_allows_ffprobe(self):
        hook = ShellGuardHook()
        event = _MockBeforeToolEvent(
            tool_name="shell_safe",
            tool_args={"command": "ffprobe video.mp4"},
        )
        import asyncio
        asyncio.get_event_loop().run_until_complete(hook.on_before_tool_call(event))
        assert event._cancelled is False


class TestAllHooksRegistered:
    def test_eight_pipeline_hooks_exist(self):
        assert len(ALL_PIPELINE_HOOKS) == 8

    def test_all_are_hook_providers(self):
        from strands.hooks import HookProvider
        for hook_cls in ALL_PIPELINE_HOOKS:
            assert issubclass(hook_cls, HookProvider), f"{hook_cls.__name__} is not a HookProvider"


# ---------------------------------------------------------------------------
# Mock event classes
# ---------------------------------------------------------------------------


class _MockBeforeNodeEvent:
    def __init__(self, node_id="test", invocation_state=None):
        self.node_id = node_id
        self.invocation_state = invocation_state or {}
        self.cancel_node = False
        self._interrupted = False

    def interrupt(self):
        self._interrupted = True


class _MockAfterNodeEvent:
    def __init__(self, node_id="test", invocation_state=None):
        self.node_id = node_id
        self.invocation_state = invocation_state or {}


class _MockBeforeToolEvent:
    def __init__(self, tool_name="test", tool_args=None):
        self.tool_name = tool_name
        self.tool_args = tool_args or {}
        self._cancelled = False
        self._result = None

    def cancel_tool(self, result=None):
        self._cancelled = True
        self._result = result
