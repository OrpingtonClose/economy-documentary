"""
Vault Reviewer Agent

A second DeepSeek v4-flash agent that reviews auditor findings for false positives.
Implements the review cycle pattern from codebase-analyzer-agent.

HTTP client: httpx (direct API calls to DeepSeek's OpenAI-compatible endpoint)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from vault_auditor_agent import AuditFinding

logger = logging.getLogger(__name__)


@dataclass
class ReviewResult:
    accepted: bool
    feedback: str
    confidence: float
    false_positive_reason: str = ""
    corrected_verdict: str = ""  # If reviewer thinks verdict should change


class VaultReviewerAgent:
    """
    Reviews auditor findings to catch false positives.
    Uses the same knowledge graph for full context.
    """

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com/v1"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = "deepseek-v4-flash"
        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        return """You are a Reviewer Agent reviewing another auditor's findings.

Your job: Determine if an auditor's finding is a REAL issue or a FALSE POSITIVE.

COMMON FALSE POSITIVE PATTERNS:
1. PascalCase vs snake_case: Effect CLASS names use PascalCase (QueueJob); kind FIELDS use snake_case (queue_job). Both are correct.
2. "Poll" terminology: The architecture deliberately uses "poll" to describe agents querying the GSA. This is NOT a contradiction with event-driven design.
3. Code examples: Illustrative pseudocode is allowed to use undefined symbols. Only flag if explicitly marked as "implementation".
4. Global State Agent: It IS documented across §02, §12, §16. Not missing.
5. VM effects: VMAllocated, VMDeallocated, VMProvisionFailed, VMObserved ARE defined in §03 and §09.5.
6. HumanInstruction: PascalCase is the class name; human_instruction is the kind field. Both correct.
7. Budget effects: budget_exceeded and budget_set are agent-emitted, not automatic infrastructure.
8. Test agent privileges: Intentional test-only capability.
9. "current phase" retry: Event-driven re-execution by the same agent, not a state machine.
10. Low-confidence parsing: Different from parse failure; different handling by design.

REVIEW CRITERIA:
- Is the finding based on a real contradiction in the text?
- Did the auditor miss context that explains the apparent inconsistency?
- Is the suggested fix actually needed?

RESPONSE FORMAT (JSON):
{
  "accepted": true|false,
  "feedback": "Detailed explanation of why this is accepted or rejected",
  "confidence": 0.0-1.0,
  "false_positive_reason": "If rejected, which pattern explains the false positive",
  "corrected_verdict": "If the finding has merit but wrong severity, suggest corrected verdict"
}
"""

    async def review_finding(
        self,
        finding: AuditFinding,
        paragraph_text: str,
        paragraph_metadata: dict,
        graph_context: str,
        project_summary: str,
    ) -> ReviewResult:
        """Review a single auditor finding."""

        user_prompt = self._build_review_prompt(
            finding, paragraph_text, paragraph_metadata,
            graph_context, project_summary,
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
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
                    return self._parse_response(content)
        except Exception as e:
            logger.error(f"Review failed for {finding.paragraph_id}: {e}")
            return ReviewResult(
                accepted=True,
                feedback=f"Review error: {e}. Defaulting to accept.",
                confidence=0.0,
            )

    def _build_review_prompt(
        self,
        finding: AuditFinding,
        paragraph_text: str,
        paragraph_metadata: dict,
        graph_context: str,
        project_summary: str,
    ) -> str:
        return f"""=== PROJECT SUMMARY ===
{project_summary}

=== GRAPH CONTEXT ===
{graph_context}

=== ORIGINAL PARAGRAPH ===
File: {paragraph_metadata.get('filename', '?')}
Section: {paragraph_metadata.get('section', '?')}

{paragraph_text}

=== AUDITOR FINDING ===
Verdict: {finding.verdict}
Category: {finding.category}
Description: {finding.description}
Evidence: {finding.evidence}
Suggested Fix: {finding.suggested_fix}
Confidence: {finding.confidence}
Reasoning: {" | ".join(finding.reasoning_chain)}

=== INSTRUCTIONS ===
Review this finding. Is it a real issue or a false positive?
Respond ONLY with the JSON format specified in your system prompt."""

    def _parse_response(self, content: str) -> ReviewResult:
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

        return ReviewResult(
            accepted=data.get("accepted", True),
            feedback=data.get("feedback", ""),
            confidence=float(data.get("confidence", 0.0)),
            false_positive_reason=data.get("false_positive_reason", ""),
            corrected_verdict=data.get("corrected_verdict", ""),
        )
