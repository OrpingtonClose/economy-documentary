"""
Vault Architect Agent

DeepSeek v4-flash powered agent that performs 11 architectural analysis checks.
Every check is LLM-driven. The graph is used only to assemble focused context
for each candidate — all reasoning is performed by the agent.

HTTP client: aiohttp (direct API calls to DeepSeek's OpenAI-compatible endpoint)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from vault_knowledge_graph import VaultKnowledgeGraph

logger = logging.getLogger(__name__)


@dataclass
class ArchitectFinding:
    check_name: str
    severity: str  # "info", "minor", "moderate", "major"
    paragraph_id: str = ""
    file: str = ""
    section: str = ""
    description: str = ""
    evidence: str = ""
    suggested_fix: str = ""
    confidence: float = 0.0
    reasoning_chain: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class VaultArchitectAgent:
    """
    DeepSeek v4-flash powered architect that analyzes structural patterns.
    """

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com/v1"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = "deepseek-v4-flash"

    def _system_prompt_for_check(self, check_name: str) -> str:
        """Return the system prompt for a specific architectural check."""
        prompts = {
            "orphaned_concept": """You are an Architecture Auditor analyzing orphaned concepts.

A concept is "orphaned" if it is mentioned in exactly one paragraph across the entire project and never defined or cited elsewhere. Orphaned concepts suggest incomplete documentation, abandoned ideas, or dead code.

You will receive:
1. A concept name
2. The single paragraph where it is mentioned
3. The project summary

TASK: Decide if this concept is truly orphaned or if it has legitimate reason to exist in only one place.

Respond with JSON:
{
  "is_orphaned": true|false,
  "description": "Why this is or is not a problem",
  "severity": "info|minor|moderate|major",
  "suggested_fix": "How to fix if it is a problem",
  "confidence": 0.0-1.0,
  "reasoning_chain": ["Step 1...", "Step 2..."]
}""",

            "concept_duplication": """You are an Architecture Auditor analyzing DRY violations.

You will receive two paragraphs from DIFFERENT files that both mention the SAME concept. The paragraphs do NOT cite each other.

TASK: Determine if these two paragraphs are independently defining/explaining the same concept (DRY violation) or if they serve different purposes.

Respond with JSON:
{
  "is_duplication": true|false,
  "description": "Why this is or is not a DRY violation",
  "severity": "info|minor|moderate|major",
  "suggested_fix": "How to fix if it is a problem",
  "confidence": 0.0-1.0,
  "reasoning_chain": ["Step 1...", "Step 2..."]
}""",

            "cyclic_dependency": """You are an Architecture Auditor analyzing citation cycles.

You will receive a chain of paragraphs where each cites the next, forming a cycle (A cites B, B cites C, ... N cites A).

TASK: Determine if this cycle is a problem (creates circular reasoning, makes documentation hard to learn) or if it is legitimate (mutual dependency).

Respond with JSON:
{
  "is_problem": true|false,
  "description": "Why this cycle is or is not a problem",
  "severity": "info|minor|moderate|major",
  "suggested_fix": "How to break the cycle if needed",
  "confidence": 0.0-1.0,
  "reasoning_chain": ["Step 1...", "Step 2..."]
}""",

            "interface_bloat": """You are an Architecture Auditor analyzing interface bloat.

You will receive a concept name and a list of ALL paragraphs that mention it, across the entire project.

TASK: Determine if this concept is doing too much (God Object anti-pattern). Does it represent multiple responsibilities? Should it be split?

Respond with JSON:
{
  "is_bloated": true|false,
  "description": "Why this is or is not bloated",
  "severity": "info|minor|moderate|major",
  "suggested_fix": "How to fix if it is bloated",
  "confidence": 0.0-1.0,
  "reasoning_chain": ["Step 1...", "Step 2..."]
}""",

            "orphaned_principle": """You are an Architecture Auditor analyzing principle enforcement.

You will receive a hard principle from §01 (Core Philosophy) and the count of how many OTHER sections cite it.

TASK: Determine if this principle is effectively enforced by the architecture, or if it is stated but ignored.

Respond with JSON:
{
  "is_orphaned": true|false,
  "description": "Why this principle is or is not enforced",
  "severity": "info|minor|moderate|major",
  "suggested_fix": "How to improve enforcement or remove the principle",
  "confidence": 0.0-1.0,
  "reasoning_chain": ["Step 1...", "Step 2..."]
}""",

            "centrality_bottleneck": """You are an Architecture Auditor analyzing documentation risk.

You will receive a paragraph that cites an unusually high number of other sections.

TASK: Determine if this high centrality creates a single point of failure (if this paragraph is wrong, it's wrong everywhere) or if it is a legitimate hub.

Respond with JSON:
{
  "is_bottleneck": true|false,
  "description": "Why this is or is not a bottleneck",
  "severity": "info|minor|moderate|major",
  "suggested_fix": "How to reduce risk if needed",
  "confidence": 0.0-1.0,
  "reasoning_chain": ["Step 1...", "Step 2..."]
}""",

            "readability_debt": """You are an Architecture Auditor analyzing readability.

You will receive a paragraph and its character count.

TASK: Determine if this paragraph length creates readability problems (too long = hard to audit, too short = fragment).

Respond with JSON:
{
  "has_debt": true|false,
  "description": "Why this paragraph length is or is not a problem",
  "severity": "info|minor|moderate|major",
  "suggested_fix": "How to fix if needed",
  "confidence": 0.0-1.0,
  "reasoning_chain": ["Step 1...", "Step 2..."]
}""",

            "naming_entropy": """You are an Architecture Auditor analyzing naming clarity.

You will receive a group of concept names that are highly similar (sharing >75% characters).

TASK: Determine if these similar names create cognitive load or confusion.

Respond with JSON:
{
  "has_entropy_problem": true|false,
  "description": "Why these names are or are not confusing",
  "severity": "info|minor|moderate|major",
  "suggested_fix": "How to improve naming clarity",
  "confidence": 0.0-1.0,
  "reasoning_chain": ["Step 1...", "Step 2..."]
}""",

            "layer_violation": """You are an Architecture Auditor analyzing layer boundaries.

You will receive:
- A paragraph from one architectural layer
- A paragraph it cites from a DIFFERENT layer
- The layer assignments

TASK: Determine if this cross-layer reference violates the principle that each layer should only depend on adjacent layers, or if it is a legitimate exception.

Respond with JSON:
{
  "is_violation": true|false,
  "description": "Why this is or is not a violation",
  "severity": "info|minor|moderate|major",
  "suggested_fix": "How to fix if it is a violation",
  "confidence": 0.0-1.0,
  "reasoning_chain": ["Step 1...", "Step 2..."]
}""",

            "missing_coverage": """You are an Architecture Auditor analyzing documentation coverage.

You will receive a concept (effect, agent, or mechanism) defined in §03 or §09.5 that is NEVER referenced in implementation docs (agent docs, handler docs, etc.).

TASK: Determine if this is a documentation gap (the concept exists but is never used) or if the references are implicit/semantic.

Respond with JSON:
{
  "has_gap": true|false,
  "description": "Why this is or is not a coverage gap",
  "severity": "info|minor|moderate|major",
  "suggested_fix": "How to close the gap if needed",
  "confidence": 0.0-1.0,
  "reasoning_chain": ["Step 1...", "Step 2..."]
}""",
        }
        return prompts.get(check_name, "You are an Architecture Auditor. Analyze the provided context and respond with JSON.")

    async def analyze(self, check_name: str, context: str, project_summary: str) -> ArchitectFinding | None:
        """Run one architectural check with focused context."""
        system_prompt = self._system_prompt_for_check(check_name)

        user_prompt = f"""=== PROJECT SUMMARY ===
{project_summary}

=== CONTEXT FOR ANALYSIS ===
{context}

=== INSTRUCTIONS ===
Analyze the provided context according to the audit criteria.
Respond ONLY with the JSON format specified in your system prompt."""

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1024,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ) as resp:
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"] or "{}"
                    return self._parse_response(content, check_name)
        except Exception as e:
            logger.error(f"Architect check {check_name} failed: {e}")
            return None

    def _parse_response(self, content: str, check_name: str) -> ArchitectFinding | None:
        """Parse JSON response into ArchitectFinding."""
        json_str = content
        if "```json" in content:
            json_str = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            json_str = content.split("```")[1].split("```")[0].strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1:
                try:
                    data = json.loads(content[start:end+1])
                except json.JSONDecodeError:
                    return None
            else:
                return None

        # Determine if this is a real finding based on check-specific flags
        is_finding = False
        flag_key = {
            "orphaned_concept": "is_orphaned",
            "concept_duplication": "is_duplication",
            "cyclic_dependency": "is_problem",
            "interface_bloat": "is_bloated",
            "orphaned_principle": "is_orphaned",
            "centrality_bottleneck": "is_bottleneck",
            "readability_debt": "has_debt",
            "naming_entropy": "has_entropy_problem",
            "layer_violation": "is_violation",
            "missing_coverage": "has_gap",
        }.get(check_name, "")

        if flag_key and not data.get(flag_key, False):
            return None  # Not a finding

        severity = data.get("severity", "info")
        if severity not in ("info", "minor", "moderate", "major"):
            severity = "info"

        return ArchitectFinding(
            check_name=check_name,
            severity=severity,
            description=data.get("description", ""),
            evidence=data.get("evidence", ""),
            suggested_fix=data.get("suggested_fix", ""),
            confidence=float(data.get("confidence") or 0.0),
            reasoning_chain=data.get("reasoning_chain", []),
        )
