"""
Plugin registry — Strands replacement for plugins/__init__.py.

The original plugins/__init__.py used ADK's BasePlugin, ContextFilterPlugin,
GlobalInstructionPlugin, ReflectAndRetryToolPlugin, and OTel hooks.
The Strands equivalent uses HookRegistry to register HookProviders.

Behavior mapping:
  - ContextFilterPlugin → ScopeHook (information boundaries)
  - GlobalInstructionPlugin → system prompt augmentation (in Agent config)
  - ReflectAndRetryToolPlugin → AfterToolCallEvent.retry()
  - OTel hooks → Strands telemetry (built-in)
  - SqliteSpanExporter → Strands telemetry exporter
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks import HookProvider, HookRegistry

logger = logging.getLogger(__name__)


def build_pipeline_hook_registry(
    hooks: list[HookProvider] | None = None,
) -> HookRegistry:
    """Build a HookRegistry with all pipeline hooks installed.

    This replaces the ADK plugin system. Each ADK plugin has a
    corresponding Strands HookProvider registered on the registry.

    Args:
        hooks: Optional additional hooks. The 8 pipeline hooks
            are always included.

    Returns:
        A configured HookRegistry ready for use with a Graph.
    """
    from strands_agents.hooks.pipeline_hooks import (
        StageContractHook,
        ImmutabilityHook,
        BudgetHook,
        ApprovalGateHook,
        ScopeHook,
        QANodeHook,
        CheckpointHook,
        ShellGuardHook,
    )
    from strands_agents.hooks.contracts import ContractEnforcer
    from strands_agents.hooks.recovery_logger import RecoveryLogger
    from strands_agents.hooks.revision_tagger import RevisionTagger
    from strands_agents.hooks.skip_if_timing_passed import SkipIfTimingPassed
    from strands_agents.hooks.reasoning_trace_hook import ReasoningTraceHook

    registry = HookRegistry()

    # Register the 8 pipeline hooks
    registry.register(StageContractHook())
    registry.register(ImmutabilityHook())
    registry.register(BudgetHook())
    registry.register(ApprovalGateHook())
    registry.register(ScopeHook())
    registry.register(QANodeHook())
    registry.register(CheckpointHook())
    registry.register(ShellGuardHook())

    # Register existing hooks from the old strands_agents codebase
    registry.register(ContractEnforcer())
    registry.register(RecoveryLogger())
    registry.register(RevisionTagger())
    registry.register(SkipIfTimingPassed())

    # Register the reasoning trace hook (replaces plugins/reasoning_trace.py)
    registry.register(ReasoningTraceHook())

    # Register any additional hooks
    if hooks:
        for hook in hooks:
            registry.register(hook)

    logger.info("Pipeline hook registry built with %d hooks", len(registry._hooks) if hasattr(registry, '_hooks') else "all")
    return registry
