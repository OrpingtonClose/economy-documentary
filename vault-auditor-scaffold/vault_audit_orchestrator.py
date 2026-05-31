"""
Vault Audit Orchestrator

The central conductor. Implements the nested loop:

OUTER LOOP: For each paragraph in the vault
  1. Build rich context from knowledge graph
  2. INNER LOOP (Auditor): DeepSeek v4-flash analyzes paragraph + context
  3. INNER LOOP (Reviewer): Second agent reviews the finding
  4. If reviewer rejects, auditor re-analyzes with feedback (up to N times)
  5. Record final verdict

FINAL PHASE: Synthesize findings into structured report

Inspired by:
- wirelessr/codebase-analyzer-agent: AgentManager with review cycles
- agentralabs/codebase: Semantic navigation through structured graphs
- Lum1104/Understand-Anything: Multi-agent pipeline with knowledge graph
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from vault_knowledge_graph import VaultKnowledgeGraph, build_knowledge_graph
from vault_auditor_agent import AuditFinding, VaultAuditorAgent
from vault_reviewer_agent import ReviewResult, VaultReviewerAgent

logger = logging.getLogger(__name__)


@dataclass
class AuditRun:
    """Complete record of one audit run."""
    started_at: str
    finished_at: str = ""
    total_paragraphs: int = 0
    findings: list[dict] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)


class VaultAuditOrchestrator:
    """
    Orchestrates the full vault audit pipeline.
    """

    def __init__(
        self,
        api_key: str,
        vault_dir: str,
        max_reviews: int = 2,
        batch_size: int = 5,
        limit: int = 0,
        concurrency: int = 3,
    ):
        self.api_key = api_key
        self.vault_dir = vault_dir
        self.max_reviews = max_reviews
        self.batch_size = batch_size
        self.limit = limit
        self.concurrency = concurrency
        self.semaphore = asyncio.Semaphore(concurrency)

        self.graph: VaultKnowledgeGraph | None = None
        self.auditor = VaultAuditorAgent(api_key)
        self.reviewer = VaultReviewerAgent(api_key)

    async def run(self) -> AuditRun:
        """Execute the full audit pipeline."""
        run_record = AuditRun(started_at=datetime.utcnow().isoformat())

        # Phase 1: Build knowledge graph
        logger.info("Phase 1: Building knowledge graph...")
        self.graph = build_knowledge_graph(self.vault_dir)
        paragraphs = [n for n in self.graph.nodes.values() if n.type == "paragraph"]
        run_record.total_paragraphs = len(paragraphs)
        logger.info(f"  Loaded {len(paragraphs)} paragraphs from {len([n for n in self.graph.nodes.values() if n.type == 'file'])} files")

        project_summary = self.graph.get_project_summary()

        # Apply limit if specified
        if self.limit > 0:
            paragraphs = paragraphs[:self.limit]
            logger.info(f"Limit applied: analyzing {self.limit} paragraphs")

        # Phase 2: Nested audit loop (concurrent with semaphore)
        logger.info("Phase 2: Starting nested audit loop...")
        findings = []
        accepted_count = 0
        rejected_count = 0
        clean_count = 0
        verdict_counts = {"CLEAN": 0, "MINOR": 0, "MODERATE": 0, "CRITICAL": 0}

        async def process_one(i: int, para) -> dict | None:
            async with self.semaphore:
                graph_context = self.graph.get_context_for_paragraph(para.id)
                finding, review = await self._audit_with_review_cycle(
                    para, graph_context, project_summary
                )
                return {
                    "index": i,
                    "para": para,
                    "finding": finding,
                    "review": review,
                }

        tasks = [process_one(i, para) for i, para in enumerate(paragraphs)]
        completed = 0
        for coro in asyncio.as_completed(tasks):
            result = await coro
            completed += 1
            i = result["index"]
            para = result["para"]
            finding = result["finding"]
            review = result["review"]

            verdict_counts[finding.verdict] = verdict_counts.get(finding.verdict, 0) + 1

            if finding.verdict == "CLEAN":
                clean_count += 1
            elif review and review.accepted:
                accepted_count += 1
                findings.append(self._serialize_finding(finding, review, para))
            elif review and not review.accepted:
                rejected_count += 1
                findings.append(self._serialize_finding(finding, review, para))

            if completed % 10 == 0:
                logger.info(f"    Progress: {completed}/{len(paragraphs)} | Clean: {clean_count} | Accepted: {accepted_count} | Rejected: {rejected_count}")

        # Phase 3: Synthesize
        logger.info("Phase 3: Synthesizing report...")
        run_record.findings = findings
        run_record.finished_at = datetime.utcnow().isoformat()
        run_record.statistics = {
            "total_paragraphs": len(paragraphs),
            "clean": clean_count,
            "accepted_findings": accepted_count,
            "rejected_findings": rejected_count,
            "verdict_distribution": verdict_counts,
            "clean_rate_pct": round(clean_count / len(paragraphs) * 100, 1) if paragraphs else 0,
        }

        return run_record

    async def _audit_with_review_cycle(
        self,
        para,
        graph_context: str,
        project_summary: str,
    ) -> tuple[AuditFinding, ReviewResult | None]:
        """
        Run auditor, then reviewer. If reviewer rejects, re-audit with feedback.
        """
        finding = await self.auditor.audit_paragraph(
            paragraph_id=para.id,
            paragraph_text=para.content,
            paragraph_metadata=para.metadata,
            graph_context=graph_context,
            project_summary=project_summary,
        )

        if finding.verdict == "CLEAN":
            return finding, None

        # Review cycle
        review = None
        for review_round in range(self.max_reviews):
            review = await self.reviewer.review_finding(
                finding=finding,
                paragraph_text=para.content,
                paragraph_metadata=para.metadata,
                graph_context=graph_context,
                project_summary=project_summary,
            )

            if review.accepted:
                break

            # Reviewer rejected - update finding with feedback and re-audit
            if review_round < self.max_reviews - 1:
                feedback = f"REVIEWER FEEDBACK (round {review_round + 1}): {review.feedback}"
                if review.false_positive_reason:
                    feedback += f" FALSE POSITIVE REASON: {review.false_positive_reason}"

                # Re-audit with reviewer feedback added to context
                enriched_context = graph_context + f"\n\n=== PREVIOUS AUDIT FEEDBACK ===\n{feedback}\n"
                finding = await self.auditor.audit_paragraph(
                    paragraph_id=para.id,
                    paragraph_text=para.content,
                    paragraph_metadata=para.metadata,
                    graph_context=enriched_context,
                    project_summary=project_summary,
                )

                if finding.verdict == "CLEAN":
                    break

        return finding, review

    def _serialize_finding(
        self,
        finding: AuditFinding,
        review: ReviewResult | None,
        para,
    ) -> dict:
        return {
            "paragraph_id": finding.paragraph_id,
            "file": para.metadata.get("filename", ""),
            "section": para.metadata.get("section", ""),
            "verdict": finding.verdict,
            "category": finding.category,
            "description": finding.description,
            "evidence": finding.evidence,
            "suggested_fix": finding.suggested_fix,
            "confidence": finding.confidence,
            "reasoning_chain": finding.reasoning_chain,
            "review": {
                "accepted": review.accepted if review else None,
                "feedback": review.feedback if review else "",
                "review_confidence": review.confidence if review else 0.0,
                "false_positive_reason": review.false_positive_reason if review else "",
                "corrected_verdict": review.corrected_verdict if review else "",
            } if review else None,
        }

    def save_report(self, run: AuditRun, output_path: str) -> None:
        """Save the full audit report to JSON."""
        report = {
            "audit_run": {
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "vault_dir": self.vault_dir,
                "total_paragraphs": run.total_paragraphs,
                "statistics": run.statistics,
            },
            "findings": run.findings,
        }
        Path(output_path).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        logger.info(f"Report saved to {output_path}")

    def print_summary(self, run: AuditRun) -> None:
        """Print human-readable summary to stdout."""
        stats = run.statistics
        print("\n" + "=" * 70)
        print("VAULT CONSISTENCY AUDIT REPORT")
        print("=" * 70)
        print(f"Started:  {run.started_at}")
        print(f"Finished: {run.finished_at}")
        print(f"Paragraphs analyzed: {stats['total_paragraphs']}")
        print(f"Clean rate: {stats['clean_rate_pct']}%")
        print()
        print("VERDICT DISTRIBUTION:")
        for v, c in stats['verdict_distribution'].items():
            print(f"  {v}: {c}")
        print()

        # Non-clean findings
        non_clean = [f for f in run.findings if f['verdict'] != 'CLEAN']
        real_issues = [f for f in non_clean if f.get('review') and f['review'].get('accepted')]
        false_positives = [f for f in non_clean if f.get('review') and not f['review'].get('accepted')]

        print(f"NON-CLEAN FINDINGS: {len(non_clean)}")
        print(f"  Accepted (real issues): {len(real_issues)}")
        print(f"  Rejected (false positives): {len(false_positives)}")
        print()

        if real_issues:
            print("REAL ISSUES:")
            for f in real_issues:
                print(f"  [{f['verdict']}] {f['file']} :: {f['section']}")
                print(f"    {f['description']}")
                if f['suggested_fix']:
                    print(f"    Fix: {f['suggested_fix']}")
                print()

        if false_positives:
            print("FALSE POSITIVES (reviewer caught):")
            for f in false_positives:
                reason = f.get('review', {}).get('false_positive_reason', 'unknown')
                print(f"  {f['file']} :: {f['section']} — {reason}")

        print("=" * 70)


# ─── Entry Point ─────────────────────────────────────────────────────────────

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Vault Consistency Auditor")
    parser.add_argument("--vault-dir", required=True, help="Path to vault directory")
    parser.add_argument("--api-key-file", default="/tmp/ds_key.txt", help="Path to file containing DeepSeek API key")
    parser.add_argument("--output", default="vault_audit_report.json", help="Output JSON path")
    parser.add_argument("--max-reviews", type=int, default=2, help="Max review cycles per paragraph")
    parser.add_argument("--limit", type=int, default=0, help="Limit to N paragraphs (0 = all)")
    parser.add_argument("--concurrency", type=int, default=3, help="Concurrent API calls")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    api_key = Path(args.api_key_file).read_text().strip()
    orchestrator = VaultAuditOrchestrator(
        api_key=api_key,
        vault_dir=args.vault_dir,
        max_reviews=args.max_reviews,
        limit=args.limit,
        concurrency=args.concurrency,
    )

    run = await orchestrator.run()
    orchestrator.save_report(run, args.output)
    orchestrator.print_summary(run)


if __name__ == "__main__":
    asyncio.run(main())
