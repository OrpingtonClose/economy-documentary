"""Reusable :class:`HookProvider` implementations shared across agents."""

from __future__ import annotations

from strands_agents.hooks.contracts import ContractEnforcer
from strands_agents.hooks.otio_contracts import OTIOContractEnforcer
from strands_agents.hooks.recovery_logger import RecoveryLogger
from strands_agents.hooks.revision_tagger import RevisionTagger
from strands_agents.hooks.skip_if_timing_passed import SkipIfTimingPassed
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

__all__ = [
    "ContractEnforcer",
    "OTIOContractEnforcer",
    "RecoveryLogger",
    "RevisionTagger",
    "SkipIfTimingPassed",
    "StageContractHook",
    "ImmutabilityHook",
    "BudgetHook",
    "ApprovalGateHook",
    "ScopeHook",
    "QANodeHook",
    "CheckpointHook",
    "ShellGuardHook",
]
