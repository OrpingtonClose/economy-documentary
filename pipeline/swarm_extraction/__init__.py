"""
Swarm extraction — multi-agent research and claim verification.

Provides LLM orchestration, tool execution, and condition management
for the documentary enrichment pipeline.
"""

from .models import AtomicCondition, CrossRef, ResearchNode
from .condition_store import ConditionStore, QuestionRegistry
from .scoring import trust_score_url, serendipity_score
from .tool_defs import NATIVE_TOOLS

__all__ = [
    "AtomicCondition",
    "CrossRef",
    "ResearchNode",
    "ConditionStore",
    "QuestionRegistry",
    "trust_score_url",
    "serendipity_score",
    "NATIVE_TOOLS",
]
