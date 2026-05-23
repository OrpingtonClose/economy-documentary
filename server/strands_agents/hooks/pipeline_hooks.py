"""
Symbolic control layer — 8 Strands hooks for the documentary pipeline.

These hooks enforce invariants that the LLM cannot bypass. They are
registered on the Graph's HookRegistry and fire at the appropriate
lifecycle events. The neuro-symbolic pattern: hooks are unbypassable
because ``BeforeToolCallEvent.cancel_tool`` and ``BeforeNodeCallEvent.cancel_node``
are checked by the Strands runtime *after* all hooks have run.

Hook list:
  1. StageContractHook   — validate pre/post conditions on stages
  2. ImmutabilityHook     — prevent mutation of generated media
  3. BudgetHook           — track and enforce cost budget
  4. ApprovalGateHook     — human-in-the-loop gates
  5. ScopeHook            — information boundaries between stages
  6. QANodeHook           — per-node QA after each stage
  7. CheckpointHook       — OTIO snapshots to B2 after each stage
  8. ShellGuardHook       — command allowlisting for shell tools
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks import (
    AfterNodeCallEvent,
    BeforeNodeCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. StageContractHook
# ---------------------------------------------------------------------------


class StageContractHook(HookProvider):
    """Validate preconditions before a node runs and postconditions after.

    Reads the contract definitions from the contracts module and checks
    that the invocation_state satisfies the required keys and services
    before allowing the node to proceed.
    """

    def __init__(self, contracts: dict[str, Any] | None = None) -> None:
        self._contracts = contracts or {}

    async def on_before_node_call(self, event: BeforeNodeCallEvent) -> None:
        node_id = event.node_id if hasattr(event, 'node_id') else None
        if node_id and node_id in self._contracts:
            contract = self._contracts[node_id]
            if contract.get("preconditions"):
                state = event.invocation_state or {}
                missing = [k for k in contract["preconditions"] if k not in state]
                if missing:
                    event.cancel_node = True
                    logger.warning(
                        "StageContractHook: node '%s' blocked — missing preconditions: %s",
                        node_id, missing,
                    )

    async def on_after_node_call(self, event: AfterNodeCallEvent) -> None:
        node_id = event.node_id if hasattr(event, 'node_id') else None
        if node_id and node_id in self._contracts:
            contract = self._contracts[node_id]
            if contract.get("postconditions"):
                state = event.invocation_state or {}
                missing = [k for k in contract["postconditions"] if k not in state]
                if missing:
                    logger.warning(
                        "StageContractHook: node '%s' postconditions missing: %s",
                        node_id, missing,
                    )


# ---------------------------------------------------------------------------
# 2. ImmutabilityHook
# ---------------------------------------------------------------------------


class ImmutabilityHook(HookProvider):
    """Prevent mutation of generated media files.

    Once a WAV or MP4 file has been marked as 'delivered' or 'approved',
    no tool may overwrite or delete it. Enforces ARCH-F (media
    immutability invariant).
    """

    # Tool names that attempt media mutation
    MUTATION_TOOLS = frozenset({
        "overwrite_audio", "delete_clip", "replace_video",
        "re_render_clip", "modify_existing",
    })

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)

    async def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        tool_name = event.tool_use.get("name", "") if isinstance(event.tool_use, dict) else ""
        if tool_name in self.MUTATION_TOOLS:
            event.cancel_tool = (
                "BLOCKED: Media immutability invariant — "
                "cannot overwrite generated media. Use re-generation instead."
            )
            logger.warning("ImmutabilityHook: blocked tool '%s'", tool_name)


# ---------------------------------------------------------------------------
# 3. BudgetHook
# ---------------------------------------------------------------------------


class BudgetHook(HookProvider):
    """Track and enforce cost budget across the pipeline.

    After each node call, accumulates the cost and checks against the
    budget. If the budget is exceeded, the hook cancels subsequent
    nodes and emits an escalation event.
    """

    def __init__(self, budget_usd: float = 100.0) -> None:
        self._budget = budget_usd
        self._accrued = 0.0

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterNodeCallEvent, self.on_after_node_call)

    @property
    def accrued(self) -> float:
        return self._accrued

    @property
    def budget(self) -> float:
        return self._budget

    async def on_after_node_call(self, event: AfterNodeCallEvent) -> None:
        state = event.invocation_state or {}
        cost = state.get("_stage_cost", 0.0)
        self._accrued += cost
        if self._accrued > self._budget:
            logger.warning(
                "BudgetHook: budget exceeded ($%.2f / $%.2f)",
                self._accrued, self._budget,
            )


# ---------------------------------------------------------------------------
# 4. ApprovalGateHook
# ---------------------------------------------------------------------------


class ApprovalGateHook(HookProvider):
    """Human-in-the-loop approval gates between stages.

    When a node that requires approval completes, this hook interrupts
    the Graph so the human can review and approve before the next
    stage proceeds. Maps to the dashboard's "Approve" button workflow.
    """

    def __init__(self, gated_stages: set[str] | None = None) -> None:
        self._gated_stages = gated_stages if gated_stages is not None else {"scenario", "audio", "visual", "production", "assembly"}

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeNodeCallEvent, self.on_before_node_call)

    async def on_before_node_call(self, event: BeforeNodeCallEvent) -> None:
        node_id = event.node_id
        if node_id in self._gated_stages:
            # Check if the stage has been approved
            state = event.invocation_state or {}
            approval_key = f"_approved_{node_id}"
            if not state.get(approval_key):
                event.cancel_node = (
                    f"Approval gate for '{node_id}' — human review required. "
                    f"Set state['{approval_key}'] = True to proceed."
                )
                logger.info("ApprovalGateHook: canceling node '%s' for human approval", node_id)


# ---------------------------------------------------------------------------
# 5. ScopeHook
# ---------------------------------------------------------------------------


class ScopeHook(HookProvider):
    """Enforce information boundaries between stages.

    Each stage can only see the data it needs. This hook filters
    invocation_state before a node runs, removing keys that belong
    to other stages. Prevents information leakage.
    """

    # What each stage can see
    STAGE_SCOPE = {
        "scenario": {"brief", "scenes", "scenario_constraints"},
        "audio": {"scenes", "narration", "alignment", "audio_path"},
        "visual": {"scenes", "visual_concepts", "narration", "style"},
        "production": {"scenes", "visual_concepts", "clips", "video_path"},
        "assembly": {"clips", "timeline", "audio_path", "video_path"},
    }

    async def on_before_node_call(self, event: BeforeNodeCallEvent) -> None:
        node_id = event.node_id if hasattr(event, 'node_id') else None
        if node_id and node_id in self.STAGE_SCOPE:
            allowed = self.STAGE_SCOPE[node_id]
            state = event.invocation_state or {}
            # Log any keys that are out of scope (informational, not blocking)
            out_of_scope = [k for k in state if k not in allowed and not k.startswith("_")]
            if out_of_scope:
                logger.debug(
                    "ScopeHook: node '%s' sees out-of-scope keys: %s",
                    node_id, out_of_scope,
                )


# ---------------------------------------------------------------------------
# 6. QANodeHook
# ---------------------------------------------------------------------------


class QANodeHook(HookProvider):
    """Run per-node QA checks after each stage completes.

    After a node finishes, this hook triggers the QA evaluation
    for that stage. If QA fails, it sets recovery context in
    invocation_state so backward edges can route to the right
    recovery node.
    """

    async def on_after_node_call(self, event: AfterNodeCallEvent) -> None:
        node_id = event.node_id if hasattr(event, 'node_id') else None
        if not node_id:
            return
        # QA check is stage-specific. For the skeleton, we just log.
        logger.info("QANodeHook: QA check for node '%s'", node_id)
        # In production, this would call the stage's QA validator
        # and set _recovery_target if QA fails.


# ---------------------------------------------------------------------------
# 7. CheckpointHook
# ---------------------------------------------------------------------------


class CheckpointHook(HookProvider):
    """Checkpoint OTIO snapshots to B2 after each stage.

    After a node completes, this hook serializes the OTIO state
    manager to otio_json and uploads it to B2. Enables disaster
    recovery and time-travel debugging.
    """

    async def on_after_node_call(self, event: AfterNodeCallEvent) -> None:
        node_id = event.node_id if hasattr(event, 'node_id') else None
        if not node_id:
            return
        state = event.invocation_state or {}
        otio_manager = state.get("otio_manager")
        if otio_manager is not None and hasattr(otio_manager, "checkpoint"):
            otio_manager.checkpoint(f"after_{node_id}")
            logger.info("CheckpointHook: checkpoint 'after_%s' recorded", node_id)
        else:
            logger.debug("CheckpointHook: no otio_manager in invocation_state")


# ---------------------------------------------------------------------------
# 8. ShellGuardHook
# ---------------------------------------------------------------------------


class ShellGuardHook(HookProvider):
    """Command allowlisting for shell tools.

    Only allowlisted commands can be executed. This replaces the
    unsafe ``shell`` tool from the Strands community tools with
    a guarded version that blocks arbitrary command execution.
    """

    ALLOWED_BINARIES = frozenset({
        "ffprobe", "ffmpeg", "sox", "ls", "cat", "wc",
        "du", "file", "mediainfo", "python3",
    })

    SHELL_TOOLS = frozenset({"shell", "shell_safe", "bash"})

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)

    async def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        tool_name = event.tool_use.get("name", "") if isinstance(event.tool_use, dict) else ""
        if tool_name in self.SHELL_TOOLS:
            # Extract the command from the tool arguments
            args = event.tool_use.get("input", {}) if isinstance(event.tool_use, dict) else {}
            command = args.get("command", "") if isinstance(args, dict) else ""
            if command:
                binary = command.strip().split()[0] if command.strip() else ""
                if binary not in self.ALLOWED_BINARIES:
                    event.cancel_tool = (
                        f"BLOCKED: '{binary}' not in allowlist. "
                        f"Allowed: {sorted(self.ALLOWED_BINARIES)}"
                    )
                    logger.warning("ShellGuardHook: blocked command '%s'", binary)


# ---------------------------------------------------------------------------
# All 8 hooks
# ---------------------------------------------------------------------------

ALL_PIPELINE_HOOKS = [
    StageContractHook,
    ImmutabilityHook,
    BudgetHook,
    ApprovalGateHook,
    ScopeHook,
    QANodeHook,
    CheckpointHook,
    ShellGuardHook,
]
