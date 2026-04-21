"""Reusable :class:`HookProvider` implementations shared across agents."""

from strands_agents.hooks.contracts import ContractEnforcer
from strands_agents.hooks.revision_tagger import RevisionTagger

__all__ = ["ContractEnforcer", "RevisionTagger"]
