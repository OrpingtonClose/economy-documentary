"""
Vault Architect Orchestrator

The central conductor for architectural analysis.
Phase 1: Build knowledge graph
Phase 2: Use graph queries to identify CANDIDATES for each check
Phase 3: For each candidate, assemble focused context and send to LLM architect
Phase 4: (Optional) Send findings to reviewer for false-positive filtering
Phase 5: Synthesize report
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from vault_knowledge_graph import GraphNode, VaultKnowledgeGraph, build_knowledge_graph
from vault_architect_agent import ArchitectFinding, VaultArchitectAgent
from vault_reviewer_agent import ReviewResult, VaultReviewerAgent

logger = logging.getLogger(__name__)


# ── Layer map for structural candidate filtering ──
LAYER_MAP = {
    "01 - Core Philosophy.md": "philosophy",
    "02 - System Topology.md": "architecture",
    "03 - Effect Type Family Complete Schemas.md": "schema",
    "04 - Rules as Prompt No State Machine No Rules Engine Code.md": "architecture",
    "05 - Event Store.md": "infrastructure",
    "06 - Projections.md": "infrastructure",
    "07 - Agent Environment and Tools.md": "agent",
    "08 - Agent Architecture pydantic-deep.md": "agent",
    "09 - Agents Per-Agent Implementations.md": "agent",
    "09.5 - Effect Parser Semantic Extraction Pipeline.md": "agent",
    "10 - Provisioner Agent.md": "agent",
    "11 - VM Worker.md": "agent",
    "12 - Data Flows.md": "architecture",
    "13 - Security Model.md": "security",
    "14 - Configuration.md": "infrastructure",
    "15 - File Structure.md": "infrastructure",
    "16 - Traceability and Observability.md": "ops",
    "17 - pydantic Ecosystem Deep Audit V7.1 Addendum.md": "agent",
    "18 - Authoring Workflow for Quasi-Deterministic Agents.md": "agent",
    "19 - Discarded Propositions and Rationale.md": "meta",
    "20 - Glossary.md": "meta",
    "A. Appendix EventStoreDB Migration Path.md": "infrastructure",
}


@dataclass
class ArchitectRun:
    started_at: str
    finished_at: str = ""
    candidates_analyzed: int = 0
    findings: list[dict] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)


class VaultArchitectOrchestrator:
    def __init__(
        self,
        api_key: str,
        vault_dir: str,
        concurrency: int = 3,
        max_candidates_per_check: int = 20,
    ):
        self.api_key = api_key
        self.vault_dir = vault_dir
        self.concurrency = concurrency
        self.max_candidates_per_check = max_candidates_per_check
        self.semaphore = asyncio.Semaphore(concurrency)

        self.graph: VaultKnowledgeGraph | None = None
        self.architect = VaultArchitectAgent(api_key)
        self.reviewer = VaultReviewerAgent(api_key)

    async def run(self) -> ArchitectRun:
        run_record = ArchitectRun(started_at=datetime.utcnow().isoformat())

        logger.info("Phase 1: Building knowledge graph...")
        self.graph = build_knowledge_graph(self.vault_dir)
        paragraphs = [n for n in self.graph.nodes.values() if n.type == "paragraph"]
        logger.info(f"  Loaded {len(paragraphs)} paragraphs from {len([n for n in self.graph.nodes.values() if n.type == 'file'])} files")

        project_summary = self.graph.get_project_summary()

        logger.info("Phase 2+3: Identifying candidates and sending to LLM architect...")
        all_candidates = self._gather_all_candidates()
        logger.info(f"  Total candidates: {len(all_candidates)}")

        findings = []
        processed = 0

        async def process_one(candidate: dict) -> ArchitectFinding | None:
            nonlocal processed
            async with self.semaphore:
                context = candidate["context"]
                check_name = candidate["check_name"]
                finding = await self.architect.analyze(check_name, context, project_summary)
                processed += 1
                if processed % 10 == 0:
                    logger.info(f"    Progress: {processed}/{len(all_candidates)}")
                return finding

        tasks = [process_one(c) for c in all_candidates]
        for coro in asyncio.as_completed(tasks):
            finding = await coro
            if finding:
                findings.append(finding)

        logger.info(f"Phase 4: Reviewing {len(findings)} findings...")
        reviewed_findings = []
        for f in findings:
            # Skip info-level findings from review (too noisy)
            if f.severity == "info":
                reviewed_findings.append(self._serialize(f, None))
                continue
            # Quick review for moderate/major
            review = await self._review_finding(f, project_summary)
            reviewed_findings.append(self._serialize(f, review))

        run_record.candidates_analyzed = len(all_candidates)
        run_record.findings = reviewed_findings
        run_record.finished_at = datetime.utcnow().isoformat()

        stats = self._compute_stats(reviewed_findings)
        run_record.statistics = stats
        return run_record

    def _gather_all_candidates(self) -> list[dict]:
        """Gather candidates for ALL checks."""
        candidates = []
        candidates.extend(self._candidates_orphaned_concepts())
        candidates.extend(self._candidates_concept_duplication())
        candidates.extend(self._candidates_cyclic_dependencies())
        candidates.extend(self._candidates_interface_bloat())
        candidates.extend(self._candidates_orphaned_principles())
        candidates.extend(self._candidates_centrality_bottlenecks())
        candidates.extend(self._candidates_readability_debt())
        candidates.extend(self._candidates_naming_entropy())
        candidates.extend(self._candidates_layer_violations())
        candidates.extend(self._candidates_missing_coverage())
        return candidates

    # ── Check 1: Orphaned Concepts ──

    def _candidates_orphaned_concepts(self) -> list[dict]:
        cands = []
        for nid, node in self.graph.nodes.items():
            if node.type != "concept":
                continue
            para_refs = [e for e in self.graph._adj_in.get(nid, []) if e.type == "mentions"]
            if len(para_refs) == 1:
                para_id = para_refs[0].source
                para = self.graph.nodes.get(para_id)
                if para:
                    cands.append({
                        "check_name": "orphaned_concept",
                        "context": f"Concept: `{node.name}`\n\nMentioned in:\nFile: {para.metadata.get('filename', '?')}\nSection: {para.metadata.get('section', '?')}\n\nParagraph text:\n{para.content[:600]}",
                        "para_id": para_id,
                    })
            if len(cands) >= self.max_candidates_per_check:
                break
        logger.info(f"  Check orphaned_concepts: {len(cands)} candidates")
        return cands

    # ── Check 2: Concept Duplication ──

    def _candidates_concept_duplication(self) -> list[dict]:
        cands = []
        concept_to_paras: dict[str, list[str]] = {}
        for nid, node in self.graph.nodes.items():
            if node.type != "concept":
                continue
            paras = [e.source for e in self.graph._adj_in.get(nid, []) if e.type == "mentions"]
            if len(paras) >= 2:
                concept_to_paras[nid] = paras

        for concept_id, para_ids in concept_to_paras.items():
            concept_name = self.graph.nodes[concept_id].name
            for i, p1 in enumerate(para_ids):
                for p2 in para_ids[i + 1:]:
                    para1 = self.graph.nodes.get(p1)
                    para2 = self.graph.nodes.get(p2)
                    if not para1 or not para2:
                        continue
                    if para1.metadata.get("filename") == para2.metadata.get("filename"):
                        continue
                    cands.append({
                        "check_name": "concept_duplication",
                        "context": f"Concept: `{concept_name}`\n\nParagraph A:\nFile: {para1.metadata.get('filename', '?')}\nSection: {para1.metadata.get('section', '?')}\n\n{para1.content[:500]}\n\n---\n\nParagraph B:\nFile: {para2.metadata.get('filename', '?')}\nSection: {para2.metadata.get('section', '?')}\n\n{para2.content[:500]}",
                        "para_id": p1,
                    })
                    if len(cands) >= self.max_candidates_per_check:
                        break
                if len(cands) >= self.max_candidates_per_check:
                    break
            if len(cands) >= self.max_candidates_per_check:
                break
        logger.info(f"  Check concept_duplication: {len(cands)} candidates")
        return cands

    # ── Check 3: Cyclic Dependencies ──

    def _candidates_cyclic_dependencies(self) -> list[dict]:
        cands = []
        adj: dict[str, set[str]] = {}
        for nid in self.graph.nodes:
            adj[nid] = set()
        for e in self.graph.edges:
            if e.type == "cites":
                adj[e.source].add(e.target)

        visited_cycles: set[tuple[str, ...]] = set()
        for start in list(adj.keys())[:50]:  # Limit to avoid explosion
            stack = [(start, [start])]
            while stack:
                node, path = stack.pop()
                if len(path) > 4:
                    continue
                for neighbor in adj.get(node, set()):
                    if neighbor == start and len(path) > 1:
                        cycle = tuple(sorted(path))
                        if cycle not in visited_cycles:
                            visited_cycles.add(cycle)
                            texts = []
                            for nid in path:
                                n = self.graph.nodes.get(nid)
                                if n:
                                    texts.append(f"File: {n.metadata.get('filename', '?')}\nSection: {n.metadata.get('section', '?')}\n{n.content[:300]}...")
                            cands.append({
                                "check_name": "cyclic_dependency",
                                "context": f"Citation cycle of {len(path)} paragraphs:\n\n" + "\n\n---\n\n".join(texts),
                                "para_id": path[0],
                            })
                            if len(cands) >= self.max_candidates_per_check:
                                break
                    elif neighbor not in path:
                        stack.append((neighbor, path + [neighbor]))
                if len(cands) >= self.max_candidates_per_check:
                    break
            if len(cands) >= self.max_candidates_per_check:
                break
        logger.info(f"  Check cyclic_dependencies: {len(cands)} candidates")
        return cands

    # ── Check 4: Interface Bloat ──

    def _candidates_interface_bloat(self) -> list[dict]:
        cands = []
        mention_counts: Counter[str] = Counter()
        for e in self.graph.edges:
            if e.type == "mentions" and e.target.startswith("concept:"):
                mention_counts[e.target] += 1

        for concept_id, count in mention_counts.most_common(self.max_candidates_per_check):
            concept = self.graph.nodes.get(concept_id)
            if not concept:
                continue
            # Collect all mentioning paragraphs
            mention_texts = []
            for e in self.graph._adj_in.get(concept_id, []):
                if e.type == "mentions":
                    para = self.graph.nodes.get(e.source)
                    if para:
                        mention_texts.append(f"[{para.metadata.get('filename', '?')} :: {para.metadata.get('section', '?')}] {para.content[:200]}...")
            cands.append({
                "check_name": "interface_bloat",
                "context": f"Concept: `{concept.name}`\nMentioned in {count} paragraphs.\n\nSample mentions:\n" + "\n".join(mention_texts[:5]),
                "para_id": "",
            })
        logger.info(f"  Check interface_bloat: {len(cands)} candidates")
        return cands

    # ── Check 5: Orphaned Principles ──

    def _candidates_orphaned_principles(self) -> list[dict]:
        cands = []
        for nid, node in self.graph.nodes.items():
            if node.type != "paragraph":
                continue
            if not node.metadata.get("filename", "").startswith("01 -"):
                continue
            citations = self.graph._adj_in.get(nid, [])
            external = [e for e in citations if e.type == "cites"]
            if len(external) == 0 and "principle" in node.content.lower():
                if "|" in node.content and ("**" in node.content or "#" in node.content):
                    cands.append({
                        "check_name": "orphaned_principle",
                        "context": f"Principle paragraph:\nFile: {node.metadata.get('filename', '?')}\nSection: {node.metadata.get('section', '?')}\n\n{node.content[:600]}\n\nExternal citations: {len(external)}",
                        "para_id": nid,
                    })
            if len(cands) >= self.max_candidates_per_check:
                break
        logger.info(f"  Check orphaned_principles: {len(cands)} candidates")
        return cands

    # ── Check 6: Centrality Bottlenecks ──

    def _candidates_centrality_bottlenecks(self) -> list[dict]:
        cands = []
        citation_counts: Counter[str] = Counter()
        for e in self.graph.edges:
            if e.type == "cites":
                citation_counts[e.source] += 1

        for para_id, count in citation_counts.most_common(self.max_candidates_per_check):
            para = self.graph.nodes.get(para_id)
            if para and count >= 3:
                cands.append({
                    "check_name": "centrality_bottleneck",
                    "context": f"Paragraph cites {count} other sections.\nFile: {para.metadata.get('filename', '?')}\nSection: {para.metadata.get('section', '?')}\n\n{para.content[:600]}",
                    "para_id": para_id,
                })
        logger.info(f"  Check centrality_bottlenecks: {len(cands)} candidates")
        return cands

    # ── Check 7: Readability Debt ──

    def _candidates_readability_debt(self) -> list[dict]:
        cands = []
        for nid, node in self.graph.nodes.items():
            if node.type != "paragraph" or node.metadata.get("is_code", False):
                continue
            text_len = len(node.content)
            if text_len > 1200:
                cands.append({
                    "check_name": "readability_debt",
                    "context": f"Paragraph length: {text_len} chars (threshold: 1200)\nFile: {node.metadata.get('filename', '?')}\nSection: {node.metadata.get('section', '?')}\n\n{node.content[:800]}...",
                    "para_id": nid,
                })
            elif text_len < 50:
                cands.append({
                    "check_name": "readability_debt",
                    "context": f"Paragraph length: {text_len} chars\nFile: {node.metadata.get('filename', '?')}\nSection: {node.metadata.get('section', '?')}\n\n{node.content}",
                    "para_id": nid,
                })
            if len(cands) >= self.max_candidates_per_check:
                break
        logger.info(f"  Check readability_debt: {len(cands)} candidates")
        return cands

    # ── Check 8: Naming Entropy ──

    def _candidates_naming_entropy(self) -> list[dict]:
        cands = []
        concept_names = [
            node.name for nid, node in self.graph.nodes.items()
            if node.type == "concept" and len(node.name) > 3
        ]

        checked = set()
        for i, name1 in enumerate(concept_names):
            if name1 in checked:
                continue
            group = [name1]
            for name2 in concept_names[i + 1:]:
                ratio = difflib.SequenceMatcher(None, name1.lower(), name2.lower()).ratio()
                if ratio > 0.75 and name1 != name2:
                    group.append(name2)
                    checked.add(name2)
            if len(group) > 1:
                checked.add(name1)
                cands.append({
                    "check_name": "naming_entropy",
                    "context": f"Similar names (>{75}% char similarity):\n{', '.join(group)}",
                    "para_id": "",
                })
            if len(cands) >= self.max_candidates_per_check:
                break
        logger.info(f"  Check naming_entropy: {len(cands)} candidates")
        return cands

    # ── Check 9: Layer Violations ──

    def _candidates_layer_violations(self) -> list[dict]:
        cands = []
        ALLOWED = {
            ("agent", "infrastructure"), ("agent", "schema"),
            ("architecture", "agent"), ("architecture", "infrastructure"),
            ("architecture", "schema"), ("security", "agent"),
            ("ops", "infrastructure"), ("ops", "agent"),
            ("meta", "any"), ("any", "meta"), ("philosophy", "any"),
        }

        for e in self.graph.edges:
            if e.type != "cites":
                continue
            src_node = self.graph.nodes.get(e.source)
            tgt_node = self.graph.nodes.get(e.target)
            if not src_node or not tgt_node:
                continue
            src_file = src_node.metadata.get("filename", "")
            tgt_file = tgt_node.metadata.get("filename", "")
            src_layer = LAYER_MAP.get(src_file, "")
            tgt_layer = LAYER_MAP.get(tgt_file, "")
            if not src_layer or not tgt_layer or src_layer == tgt_layer:
                continue
            if (src_layer, tgt_layer) in ALLOWED or (src_layer, "any") in ALLOWED:
                continue
            cands.append({
                "check_name": "layer_violation",
                "context": f"Cross-layer reference:\nSource layer: {src_layer} ({src_file})\nTarget layer: {tgt_layer} ({tgt_file})\n\nSource paragraph:\n{src_node.content[:400]}...\n\nTarget paragraph:\n{tgt_node.content[:400]}...",
                "para_id": e.source,
            })
            if len(cands) >= self.max_candidates_per_check:
                break
        logger.info(f"  Check layer_violations: {len(cands)} candidates")
        return cands

    # ── Check 10: Missing Coverage ──

    def _candidates_missing_coverage(self) -> list[dict]:
        cands = []
        for nid, node in self.graph.nodes.items():
            if node.type != "concept":
                continue
            mentions = self.graph._adj_in.get(nid, [])
            # Check if mentioned outside §03 and §09.5
            external = [
                e for e in mentions
                if e.type == "mentions"
                and not e.source.startswith("03 -")
                and not e.source.startswith("09.5")
            ]
            if len(external) == 0 and len(mentions) > 0:
                # Only if it IS in §03 or §09.5
                if any(e.source.startswith("03 -") or e.source.startswith("09.5") for e in mentions):
                    cands.append({
                        "check_name": "missing_coverage",
                        "context": f"Concept: `{node.name}`\nDefined in: schema/parser docs\nNever referenced in: agent/handler/ops docs\n\nMentioning paragraphs:\n" + "\n".join(
                            f"  {self.graph.nodes.get(e.source, GraphNode('', '', '')).metadata.get('filename', '?')}"
                            for e in mentions[:5]
                        ),
                        "para_id": "",
                    })
            if len(cands) >= self.max_candidates_per_check:
                break
        logger.info(f"  Check missing_coverage: {len(cands)} candidates")
        return cands

    # ── Review ──

    async def _review_finding(self, finding: ArchitectFinding, project_summary: str) -> ReviewResult:
        """Quick review of architectural findings."""
        review_prompt = f"""=== PROJECT SUMMARY ===
{project_summary}

=== ARCHITECT FINDING ===
Check: {finding.check_name}
Severity: {finding.severity}
Description: {finding.description}
Evidence: {finding.evidence}
Suggested Fix: {finding.suggested_fix}
Reasoning: {" | ".join(finding.reasoning_chain)}

=== INSTRUCTIONS ===
Is this a real architectural issue or a false positive?
Consider: Does the evidence support the claim? Is the suggested fix actually needed?

Respond with JSON:
{{
  "accepted": true|false,
  "feedback": "Detailed explanation",
  "confidence": 0.0-1.0,
  "false_positive_reason": "If rejected, explain why"
}}"""
        try:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.reviewer.system_prompt},
                    {"role": "user", "content": review_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 512,
            }
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
                    return self.reviewer._parse_response(content)
        except Exception as e:
            logger.error(f"Review failed: {e}")
            return ReviewResult(accepted=True, feedback=f"Review error: {e}", confidence=0.0)

    @property
    def model(self):
        return self.architect.model

    @property
    def base_url(self):
        return self.architect.base_url

    def _serialize(self, finding: ArchitectFinding, review: ReviewResult | None) -> dict:
        return {
            "check_name": finding.check_name,
            "severity": finding.severity,
            "description": finding.description,
            "evidence": finding.evidence,
            "suggested_fix": finding.suggested_fix,
            "confidence": finding.confidence,
            "reasoning_chain": finding.reasoning_chain,
            "review": {
                "accepted": review.accepted if review else None,
                "feedback": review.feedback if review else "",
                "false_positive_reason": review.false_positive_reason if review else "",
            } if review else None,
        }

    def _compute_stats(self, findings: list[dict]) -> dict:
        severities = Counter(f["severity"] for f in findings)
        checks = Counter(f["check_name"] for f in findings)
        accepted = [f for f in findings if f.get("review") and f["review"].get("accepted")]
        rejected = [f for f in findings if f.get("review") and not f["review"].get("accepted")]
        return {
            "total_findings": len(findings),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "by_severity": dict(severities),
            "by_check": dict(checks),
        }

    def save_report(self, run: ArchitectRun, output_path: str) -> None:
        report = {
            "architect_run": {
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "vault_dir": self.vault_dir,
                "candidates_analyzed": run.candidates_analyzed,
                "statistics": run.statistics,
            },
            "findings": run.findings,
        }
        Path(output_path).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        logger.info(f"Report saved to {output_path}")

    def print_summary(self, run: ArchitectRun) -> None:
        stats = run.statistics
        print("\n" + "=" * 70)
        print("VAULT ARCHITECTURAL ANALYSIS REPORT")
        print("=" * 70)
        print(f"Started:  {run.started_at}")
        print(f"Finished: {run.finished_at}")
        print(f"Candidates analyzed: {run.candidates_analyzed}")
        print(f"Findings: {stats['total_findings']}")
        print(f"  Accepted: {stats['accepted']}")
        print(f"  Rejected: {stats['rejected']}")
        print()
        print("BY SEVERITY:")
        for s, c in stats['by_severity'].items():
            print(f"  {s}: {c}")
        print()
        print("BY CHECK:")
        for c, n in stats['by_check'].items():
            print(f"  {c}: {n}")

        real = [f for f in run.findings if not f.get("review") or (f.get("review") or {}).get("accepted")]
        if real:
            print(f"\nREAL FINDINGS ({len(real)}):")
            for f in real:
                rev = f" [review: {f['review']['false_positive_reason']}]" if (f.get("review") or {}).get("false_positive_reason") else ""
                print(f"  [{f['severity']}] {f['check_name']}: {f['description'][:100]}{rev}")
        print("=" * 70)


# ── Entry Point ──

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Vault Architectural Analysis")
    parser.add_argument("--vault-dir", required=True)
    parser.add_argument("--api-key-file", default="/tmp/ds_key.txt")
    parser.add_argument("--output", default="vault_architect_report.json")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    api_key = Path(args.api_key_file).read_text().strip()
    orchestrator = VaultArchitectOrchestrator(
        api_key=api_key,
        vault_dir=args.vault_dir,
        concurrency=args.concurrency,
        max_candidates_per_check=args.max_candidates,
    )

    run = await orchestrator.run()
    orchestrator.save_report(run, args.output)
    orchestrator.print_summary(run)


if __name__ == "__main__":
    asyncio.run(main())
