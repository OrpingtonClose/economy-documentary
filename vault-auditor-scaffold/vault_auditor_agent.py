"""
Vault Auditor Agent

A DeepSeek v4-flash powered agent that audits individual paragraphs
for cross-document consistency. Uses the knowledge graph for context
assembly and supports multi-step reasoning.

HTTP client: httpx (direct API calls to DeepSeek's OpenAI-compatible endpoint)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class AuditFinding:
    paragraph_id: str
    verdict: str  # "CLEAN", "MINOR", "MODERATE", "CRITICAL"
    category: str  # "naming", "reference", "contradiction", "missing", "terminology"
    description: str
    evidence: str
    suggested_fix: str
    confidence: float = 0.0
    reasoning_chain: list[str] = field(default_factory=list)


class VaultAuditorAgent:
    """
    DeepSeek v4-flash powered auditor that analyzes one paragraph at a time
    with full project context assembled from the knowledge graph.
    """

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com/v1"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = "deepseek-v4-flash"
        self.system_prompt = self._build_system_prompt()
        self.findings: list[AuditFinding] = []

    def _build_system_prompt(self) -> str:
        return """You are an Architecture Consistency Auditor for a documentary pipeline architecture vault.

Your job: Analyze ONE paragraph at a time for inconsistencies with the rest of the project.

You have access to a knowledge graph of the entire project. You will receive:
1. The paragraph under audit
2. Rich context assembled from the knowledge graph (same-file context, cited sections, mentioned concepts, related paragraphs)
3. A project summary

AUDIT CHECKLIST:
1. NAMING CONSISTENCY: Are effect names, agent names, or concepts used consistently?
   - Effect classes use PascalCase (QueueJob) while kind fields use snake_case (queue_job)
   - Both are correct; flag only if the same context uses both forms interchangeably
2. CROSS-REFERENCES: Do §NN references point to real sections? Do wikilinks resolve?
3. CONTRADICTIONS: Does this paragraph contradict any other paragraph in the project?
4. UNDEFINED TERMS: Does it use terms not defined anywhere else?
5. MISSING REFERENCES: Should it cite another section but doesn't?
6. ARCHITECTURAL PRINCIPLES: Does it violate any of the 12 hard principles?

VERDICT SCALE:
- CLEAN: No issues found
- MINOR: Stylistic inconsistency, doesn't affect correctness
- MODERATE: Missing reference, unclear term, or mild contradiction
- CRITICAL: Direct contradiction, undefined critical term, or architectural violation

RESPONSE FORMAT (JSON):
{
  "verdict": "CLEAN|MINOR|MODERATE|CRITICAL",
  "category": "naming|reference|contradiction|missing|terminology|architecture",
  "description": "One-sentence summary of the issue",
  "evidence": "Quote from the paragraph and the contradicting evidence",
  "suggested_fix": "How to fix it",
  "confidence": 0.0-1.0,
  "reasoning_chain": ["Step 1: I checked...", "Step 2: I found...", "Step 3: I conclude..."]
}

If CLEAN, all other fields except reasoning_chain should be empty/null.

IMPORTANT:
- Code examples are illustrative pseudocode unless explicitly marked as "implementation"
- PascalClass names refer to class definitions; snake_case refers to kind fields
- "Poll" is deliberate architecture terminology (tick-driven agents)
- The Global State Agent IS documented in §02, §12, §16
- The vault uses both naming conventions legitimately; do not flag PascalCase in class contexts
"""

    async def audit_paragraph(
        self,
        paragraph_id: str,
        paragraph_text: str,
        paragraph_metadata: dict,
        graph_context: str,
        project_summary: str,
    ) -> AuditFinding:
        """Audit a single paragraph with full project context."""

        user_prompt = self._build_audit_prompt(
            paragraph_id, paragraph_text, paragraph_metadata,
            graph_context, project_summary,
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
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
                    finding = self._parse_response(content, paragraph_id)
                    return finding
        except Exception as e:
            logger.error(f"Audit failed for {paragraph_id}: {e}")
            return AuditFinding(
                paragraph_id=paragraph_id,
                verdict="CLEAN",
                category="",
                description=f"Audit error: {e}",
                evidence="",
                suggested_fix="",
                confidence=0.0,
                reasoning_chain=["Error during API call"],
            )

    def _build_audit_prompt(
        self,
        paragraph_id: str,
        paragraph_text: str,
        paragraph_metadata: dict,
        graph_context: str,
        project_summary: str,
    ) -> str:
        return f"""=== PROJECT SUMMARY ===
{project_summary}

=== GRAPH CONTEXT FOR THIS PARAGRAPH ===
{graph_context}

=== PARAGRAPH UNDER AUDIT ===
File: {paragraph_metadata.get('filename', '?')}
Section: {paragraph_metadata.get('section', '?')}
ID: {paragraph_id}

{paragraph_text}

=== INSTRUCTIONS ===
Analyze this paragraph against the full project context. Check for:
1. Naming inconsistencies (same concept named differently)
2. Cross-reference validity (do §NN refs exist?)
3. Contradictions with other docs
4. Undefined terms
5. Missing references
6. Principle violations

Respond ONLY with the JSON format specified in your system prompt."""

    def _parse_response(self, content: str, paragraph_id: str) -> AuditFinding:
        """Parse JSON response into AuditFinding."""
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
                    data = {}
            else:
                data = {}

        verdict = data.get("verdict", "CLEAN")
        if verdict not in ("CLEAN", "MINOR", "MODERATE", "CRITICAL"):
            verdict = "CLEAN"

        return AuditFinding(
            paragraph_id=paragraph_id,
            verdict=verdict,
            category=data.get("category", ""),
            description=data.get("description", ""),
            evidence=data.get("evidence", ""),
            suggested_fix=data.get("suggested_fix", ""),
            confidence=float(data.get("confidence") or 0.0),
            reasoning_chain=data.get("reasoning_chain", []),
        )
