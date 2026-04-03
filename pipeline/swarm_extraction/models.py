"""
Data models for the enrichment pipeline.
Adapted from deep-search-portal's tools/models.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CrossRef:
    """Directional link between two conditions in the knowledge net."""
    relation: str   # "confirms" | "contradicts" | "related"
    target_idx: int
    similarity: float = 0.0


@dataclass
class AtomicCondition:
    """A single research finding — the atom of the knowledge store."""
    fact: str
    source_url: str = ""
    confidence: float = 0.5
    angle: str = ""
    domain: str = ""
    is_serendipitous: bool = False
    trust_score: float = 0.5
    serendipity_score_val: float = 0.0
    entities: list[str] = field(default_factory=list)
    verification_status: str = ""  # verified | speculative | fabricated | overconfident | ""
    publication_date: str = ""
    author: str = ""
    content_type: str = ""    # academic_paper | news | forum_post | government_data
    source_type: str = ""     # fred | perplexity | exa | tavily | wolfram | web
    cross_refs: list[CrossRef] = field(default_factory=list)

    # Link back to the original documentary claim
    claim_id: str = ""
    scene_num: int = 0

    def to_text(self) -> str:
        parts = [f"- {self.fact}"]
        if self.source_url:
            parts[0] += f" [source: {self.source_url}]"
        if self.confidence != 0.5:
            parts[0] += f" (conf: {self.confidence:.1f})"
        if self.verification_status:
            parts[0] += f" [{self.verification_status.upper()}]"
        if self.source_type:
            parts[0] += f" [via: {self.source_type}]"
        return parts[0]


@dataclass
class ResearchNode:
    """A node in the research exploration net."""
    id: str
    question: str
    context: str
    depth: int
    pressure: float
    parent_id: Optional[str] = None
    status: str = "pending"  # pending | researching | done | pruned
    connected_to: list[str] = field(default_factory=list)

    def __lt__(self, other: ResearchNode) -> bool:
        return self.pressure > other.pressure


@dataclass
class QueryComprehension:
    """Deep semantic understanding of a research query.

    Produced once at pipeline start.  Maps the full knowledge territory:
    entities, domains, implicit questions, adjacent territories, and
    what kinds of deep knowledge would be valuable.
    """
    entities: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    implicit_questions: list[str] = field(default_factory=list)
    adjacent_territories: list[str] = field(default_factory=list)
    relevance_keywords: list[str] = field(default_factory=list)
    deep_knowledge_targets: list[str] = field(default_factory=list)
    semantic_summary: str = ""
    intent_type: str = "informational"
    core_need: str = ""


@dataclass
class ToolTrace:
    """Record of a single tool invocation during verification."""
    turn: int
    tool_name: str
    arguments: dict = field(default_factory=dict)
    result_snippet: str = ""       # first 500 chars of result
    result_length: int = 0         # full result length
    duration_sec: float = 0.0
    was_duplicate: bool = False
    error: str = ""

    def to_text(self) -> str:
        if self.was_duplicate:
            return f"  T{self.turn} {self.tool_name}(…) → DUPLICATE SKIPPED"
        status = f"{self.result_length} chars, {self.duration_sec:.1f}s"
        if self.error:
            status = f"ERROR: {self.error}"
        return f"  T{self.turn} {self.tool_name}({json.dumps(self.arguments, default=str)[:120]}) → {status}"


@dataclass
class ReasoningStep:
    """Snapshot of LLM reasoning at a given turn."""
    turn: int
    content: str                   # the LLM's text output for this turn
    tool_calls_requested: int = 0  # how many tool calls the LLM asked for
    conditions_extracted: int = 0  # conditions parsed at AoT contraction
    conditions_admitted: int = 0   # conditions admitted by store
    novelty: float = -1.0          # -1 = not measured this turn
    action: str = ""               # research | contraction | final_extraction | saturation_stop


@dataclass
class EnrichmentResult:
    """Result from enriching a single claim."""
    claim_id: str
    original_text: str
    conditions: list[AtomicCondition] = field(default_factory=list)
    verification_status: str = ""  # verified | partial | disputed | unverified
    confidence: float = 0.5
    sources: list[str] = field(default_factory=list)
    counterarguments: list[str] = field(default_factory=list)
    entities_discovered: list[dict] = field(default_factory=list)
    relationships: list[dict] = field(default_factory=list)
    turns_used: int = 0
    tool_calls_made: int = 0
    error: str = ""
    # New: full trace
    tool_trace: list[ToolTrace] = field(default_factory=list)
    reasoning_trace: list[ReasoningStep] = field(default_factory=list)
