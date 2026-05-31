"""
Vault Graph Analyzer — Structural analysis (no LLM required).

Implements 11 architectural analysis checks using the knowledge graph:
1. Dead Code / Orphaned Concepts
2. Concept Duplication (DRY)
3. Cyclic Dependencies
4. Interface Bloat
5. Orphaned Principles
6. Graph Centrality Bottlenecks
7. Readability Debt
8. Missing Coverage Gaps
9. Naming Entropy
10. Architectural Layer Violations
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from vault_knowledge_graph import GraphEdge, GraphNode, VaultKnowledgeGraph


@dataclass
class GraphFinding:
    check_name: str
    severity: str  # "info", "minor", "moderate", "major"
    paragraph_id: str = ""
    file: str = ""
    section: str = ""
    description: str = ""
    evidence: str = ""
    suggested_fix: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class VaultGraphAnalyzer:
    """Structural analysis of the knowledge graph. No LLM calls."""

    def __init__(self, graph: VaultKnowledgeGraph):
        self.graph = graph
        self.findings: list[GraphFinding] = []

    def run_all_checks(self) -> list[GraphFinding]:
        """Run all structural checks and return findings."""
        self.findings = []
        self.check_orphaned_concepts()
        self.check_cyclic_dependencies()
        self.check_interface_bloat()
        self.check_orphaned_principles()
        self.check_centrality_bottlenecks()
        self.check_readability_debt()
        self.check_naming_entropy()
        self.check_layer_violations()
        self.check_concept_duplication()
        self.check_missing_coverage_gaps()
        return self.findings

    # ── 1. Dead Code / Orphaned Concepts ────────────────────────────────────

    def check_orphaned_concepts(self) -> None:
        """Concepts mentioned in only one paragraph (orphaned)."""
        for nid, node in self.graph.nodes.items():
            if node.type != "concept":
                continue
            # Count paragraphs that mention this concept
            para_refs = [e for e in self.graph._adj_in.get(nid, []) if e.type == "mentions"]
            if len(para_refs) == 1:
                para_id = para_refs[0].source
                para = self.graph.nodes.get(para_id)
                if para:
                    self.findings.append(GraphFinding(
                        check_name="orphaned_concept",
                        severity="minor",
                        paragraph_id=para_id,
                        file=para.metadata.get("filename", ""),
                        section=para.metadata.get("section", ""),
                        description=f"Concept `{node.name}` is mentioned in exactly one paragraph — likely orphaned or undocumented elsewhere.",
                        evidence=f"Only mentioned in {para.metadata.get('filename', '?')} :: {para.metadata.get('section', '?')}",
                        suggested_fix=f"Either remove `{node.name}` if obsolete, or document it in at least one other section.",
                        metadata={"concept": node.name, "mention_count": 1},
                    ))
            elif len(para_refs) == 0:
                self.findings.append(GraphFinding(
                    check_name="orphaned_concept",
                    severity="info",
                    description=f"Concept `{node.name}` exists as a node but has zero mentions.",
                    metadata={"concept": node.name, "mention_count": 0},
                ))

    # ── 2. Concept Duplication (DRY) ────────────────────────────────────────

    def check_concept_duplication(self) -> None:
        """Detect paragraphs that describe the same concept without cross-referencing each other."""
        # Group paragraphs by shared concepts
        concept_to_paras: dict[str, list[str]] = {}
        for nid, node in self.graph.nodes.items():
            if node.type != "concept":
                continue
            paras = [e.source for e in self.graph._adj_in.get(nid, []) if e.type == "mentions"]
            if len(paras) >= 2:
                concept_to_paras[nid] = paras

        # For concepts with multiple mentions, check if the paragraphs cite each other
        for concept_id, para_ids in concept_to_paras.items():
            if len(para_ids) < 2:
                continue
            concept_name = self.graph.nodes[concept_id].name
            for i, p1 in enumerate(para_ids):
                for p2 in para_ids[i + 1:]:
                    # Check if p1 cites p2 or p2 cites p1
                    cites_1_to_2 = any(
                        e.target == p2 for e in self.graph._adj_out.get(p1, []) if e.type == "cites"
                    )
                    cites_2_to_1 = any(
                        e.target == p1 for e in self.graph._adj_out.get(p2, []) if e.type == "cites"
                    )
                    if not cites_1_to_2 and not cites_2_to_1:
                        para1 = self.graph.nodes.get(p1)
                        para2 = self.graph.nodes.get(p2)
                        if para1 and para2:
                            # Only flag if they're in different files
                            if para1.metadata.get("filename") != para2.metadata.get("filename"):
                                self.findings.append(GraphFinding(
                                    check_name="concept_duplication",
                                    severity="minor",
                                    paragraph_id=p1,
                                    file=para1.metadata.get("filename", ""),
                                    section=para1.metadata.get("section", ""),
                                    description=f"Concept `{concept_name}` is described in both `{para1.metadata.get('filename', '?')}` and `{para2.metadata.get('filename', '?')}` without cross-referencing each other.",
                                    evidence=f"File 1: {para1.metadata.get('filename', '?')} :: {para1.metadata.get('section', '?')}\nFile 2: {para2.metadata.get('filename', '?')} :: {para2.metadata.get('section', '?')}",
                                    suggested_fix=f"Add a cross-reference link from one file to the other, or consolidate the definition of `{concept_name}` into a single authoritative location.",
                                    metadata={"concept": concept_name, "para1": p1, "para2": p2},
                                ))

    # ── 3. Cyclic Dependencies ──────────────────────────────────────────────

    def check_cyclic_dependencies(self) -> None:
        """Find citation cycles (A cites B, B cites A, or longer)."""
        # Build citation adjacency
        adj: dict[str, set[str]] = {}
        for nid in self.graph.nodes:
            adj[nid] = set()
        for e in self.graph.edges:
            if e.type == "cites":
                adj[e.source].add(e.target)

        # DFS for cycles up to length 5
        visited_cycles: set[tuple[str, ...]] = set()
        for start in adj:
            stack = [(start, [start])]
            while stack:
                node, path = stack.pop()
                if len(path) > 5:
                    continue
                for neighbor in adj.get(node, set()):
                    if neighbor == start and len(path) > 1:
                        cycle = tuple(sorted(path))
                        if cycle not in visited_cycles:
                            visited_cycles.add(cycle)
                            self._report_cycle(path)
                    elif neighbor not in path:
                        stack.append((neighbor, path + [neighbor]))

    def _report_cycle(self, path: list[str]) -> None:
        """Report a citation cycle."""
        files = []
        for nid in path:
            node = self.graph.nodes.get(nid)
            if node:
                fname = node.metadata.get("filename", node.id)
                files.append(fname)
        unique_files = list(dict.fromkeys(files))  # preserve order, dedupe
        self.findings.append(GraphFinding(
            check_name="cyclic_dependency",
            severity="moderate",
            description=f"Circular citation detected among {len(unique_files)} sections.",
            evidence=f"Cycle: {' → '.join(unique_files)}",
            suggested_fix="Break the cycle by choosing one section as the authoritative definition and converting other references to one-way citations.",
            metadata={"cycle_files": unique_files},
        ))

    # ── 4. Interface Bloat ──────────────────────────────────────────────────

    def check_interface_bloat(self) -> None:
        """Concepts mentioned in an unusually high number of paragraphs."""
        mention_counts: Counter[str] = Counter()
        for e in self.graph.edges:
            if e.type == "mentions" and e.target.startswith("concept:"):
                mention_counts[e.target] += 1

        if not mention_counts:
            return

        mean = sum(mention_counts.values()) / len(mention_counts)
        variance = sum((c - mean) ** 2 for c in mention_counts.values()) / len(mention_counts)
        std = math.sqrt(variance)
        threshold = mean + 2 * std

        for concept_id, count in mention_counts.most_common():
            if count < threshold:
                break
            concept = self.graph.nodes.get(concept_id)
            if concept:
                self.findings.append(GraphFinding(
                    check_name="interface_bloat",
                    severity="info",
                    description=f"Concept `{concept.name}` is mentioned in {count} paragraphs (threshold: {threshold:.1f}). May be doing too much.",
                    evidence=f"Mean mentions per concept: {mean:.1f}, std: {std:.1f}. This concept appears {count} times.",
                    suggested_keep=f"Consider splitting `{concept.name}` into more specific sub-concepts if it represents multiple ideas.",
                    metadata={"concept": concept.name, "mentions": count, "threshold": threshold},
                ))

    # ── 5. Orphaned Principles ──────────────────────────────────────────────

    def check_orphaned_principles(self) -> None:
        """Find principles from §01 that are never referenced elsewhere."""
        # Find principle concepts (look for principle-related terms in §01)
        principle_paras = []
        for nid, node in self.graph.nodes.items():
            if node.type == "paragraph" and node.metadata.get("filename", "").startswith("01 -"):
                principle_paras.append(nid)

        # For each principle paragraph, check if it's cited by other files
        for para_id in principle_paras:
            para = self.graph.nodes.get(para_id)
            if not para:
                continue
            # Count citations from OTHER files
            citations = self.graph._adj_in.get(para_id, [])
            external_cites = [
                e for e in citations if e.type == "cites"
                and self.graph.nodes.get(e.source, GraphNode("", "", "")).metadata.get("filename", "") != para.metadata.get("filename", "")
            ]
            if len(external_cites) == 0 and "principle" in para.content.lower():
                # Check if the paragraph text contains a numbered principle
                if re.search(r'\|\s*\d+\s*\|\s*\*\*', para.content):
                    self.findings.append(GraphFinding(
                        check_name="orphaned_principle",
                        severity="minor",
                        paragraph_id=para_id,
                        file=para.metadata.get("filename", ""),
                        section=para.metadata.get("section", ""),
                        description=f"Principle in §01 is never cited by any other section. It may not be enforced by the architecture.",
                        evidence=f"Paragraph in {para.metadata.get('filename', '?')} :: {para.metadata.get('section', '?')} has zero external citations.",
                        suggested_fix=f"Add explicit citations to this principle from sections that should enforce it, or consider removing it if it is no longer relevant.",
                        metadata={"principle_preview": para.content[:100]},
                    ))

    # ── 6. Graph Centrality Bottlenecks ─────────────────────────────────────

    def check_centrality_bottlenecks(self) -> None:
        """Find paragraphs/sections with unusually high citation count (risk concentrators)."""
        # Count outgoing citations per paragraph
        citation_counts: Counter[str] = Counter()
        for e in self.graph.edges:
            if e.type == "cites":
                citation_counts[e.source] += 1

        if not citation_counts:
            return

        mean = sum(citation_counts.values()) / len(citation_counts)
        variance = sum((c - mean) ** 2 for c in citation_counts.values()) / len(citation_counts)
        std = math.sqrt(variance)
        threshold = mean + 2 * std

        for para_id, count in citation_counts.most_common(10):
            if count < threshold:
                break
            para = self.graph.nodes.get(para_id)
            if para:
                self.findings.append(GraphFinding(
                    check_name="centrality_bottleneck",
                    severity="info",
                    paragraph_id=para_id,
                    file=para.metadata.get("filename", ""),
                    section=para.metadata.get("section", ""),
                    description=f"This paragraph cites {count} other sections. High centrality = single point of documentation failure.",
                    evidence=f"Mean citations per paragraph: {mean:.1f}, std: {std:.1f}. This paragraph has {count}.",
                    suggested_fix="Consider splitting this paragraph or moving cited definitions closer to their usage to reduce dependency radius.",
                    metadata={"citation_count": count, "threshold": threshold},
                ))

    # ── 7. Readability Debt ─────────────────────────────────────────────────

    def check_readability_debt(self) -> None:
        """Flag paragraphs that are too long or too short."""
        for nid, node in self.graph.nodes.items():
            if node.type != "paragraph":
                continue
            text_len = len(node.content)
            is_code = node.metadata.get("is_code", False)

            if is_code:
                continue  # Code blocks can be long

            if text_len > 1200:
                self.findings.append(GraphFinding(
                    check_name="readability_debt",
                    severity="minor",
                    paragraph_id=nid,
                    file=node.metadata.get("filename", ""),
                    section=node.metadata.get("section", ""),
                    description=f"Paragraph is {text_len} chars — unusually long. Consider splitting into smaller paragraphs.",
                    evidence=f"Length: {text_len} chars (threshold: 1200)",
                    suggested_fix="Split this paragraph at logical boundaries (e.g., one idea per paragraph).",
                    metadata={"length": text_len},
                ))
            elif text_len < 50 and not is_code:
                self.findings.append(GraphFinding(
                    check_name="readability_debt",
                    severity="info",
                    paragraph_id=nid,
                    file=node.metadata.get("filename", ""),
                    section=node.metadata.get("section", ""),
                    description=f"Paragraph is only {text_len} chars — unusually short. May be a fragment.",
                    evidence=f"Length: {text_len} chars",
                    suggested_fix="Merge with adjacent paragraph or expand to a complete sentence.",
                    metadata={"length": text_len},
                ))

    # ── 8. Missing Coverage Gaps ────────────────────────────────────────────

    def check_missing_coverage_gaps(self) -> None:
        """Check that all effects/agents defined in §03 have implementations in other files."""
        # Get all effects from §03
        effect_concepts = {
            nid for nid, node in self.graph.nodes.items()
            if node.type == "concept" and any(
                e.source.startswith("03 -") for e in self.graph._adj_in.get(nid, [])
            )
        }

        for effect_id in effect_concepts:
            effect = self.graph.nodes.get(effect_id)
            if not effect:
                continue
            # Check if mentioned outside §03
            mentions = self.graph._adj_in.get(effect_id, [])
            external = [
                e for e in mentions
                if not e.source.startswith("03 -") and not e.source.startswith("09.5")
            ]
            if len(external) == 0:
                self.findings.append(GraphFinding(
                    check_name="missing_coverage",
                    severity="info",
                    description=f"Effect/concept `{effect.name}` is defined in §03 but never referenced in implementation docs.",
                    evidence=f"No mentions outside §03 / §09.5.",
                    suggested_fix=f"Add a reference to `{effect.name}` in the relevant agent or handler documentation.",
                    metadata={"concept": effect.name},
                ))

    # ── 9. Naming Entropy ───────────────────────────────────────────────────

    def check_naming_entropy(self) -> None:
        """Detect sets of names that are too similar (cognitive load)."""
        import difflib

        concept_names = [
            node.name for nid, node in self.graph.nodes.items()
            if node.type == "concept" and len(node.name) > 3
        ]

        similarity_groups: list[list[str]] = []
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
                similarity_groups.append(group)

        for group in similarity_groups:
            self.findings.append(GraphFinding(
                check_name="naming_entropy",
                severity="info",
                description=f"Similar names may cause cognitive load: {', '.join(group)}",
                evidence=f"Names share >75% character similarity.",
                suggested_fix="Consider renaming to make distinctions clearer, or document the difference explicitly.",
                metadata={"similar_names": group},
            ))

    # ── 10. Architectural Layer Violations ──────────────────────────────────

    def check_layer_violations(self) -> None:
        """Flag references that cross layer boundaries inappropriately."""
        # Define layer assignments
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

        # Allowed cross-layer references (some are legitimate)
        ALLOWED_PAIRS = {
            ("agent", "infrastructure"),
            ("agent", "schema"),
            ("architecture", "agent"),
            ("architecture", "infrastructure"),
            ("architecture", "schema"),
            ("security", "agent"),
            ("ops", "infrastructure"),
            ("ops", "agent"),
            ("meta", "any"),
            ("any", "meta"),
            ("philosophy", "any"),
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
            src_layer = LAYER_MAP.get(src_file, "unknown")
            tgt_layer = LAYER_MAP.get(tgt_file, "unknown")

            if src_layer == tgt_layer:
                continue
            if src_layer == "unknown" or tgt_layer == "unknown":
                continue

            # Check if this pair is allowed
            is_allowed = (
                (src_layer, tgt_layer) in ALLOWED_PAIRS
                or (src_layer, "any") in ALLOWED_PAIRS
                or ("any", tgt_layer) in ALLOWED_PAIRS
            )

            if not is_allowed:
                self.findings.append(GraphFinding(
                    check_name="layer_violation",
                    severity="info",
                    paragraph_id=e.source,
                    file=src_file,
                    section=src_node.metadata.get("section", ""),
                    description=f"Cross-layer reference from `{src_layer}` layer to `{tgt_layer}` layer.",
                    evidence=f"{src_file} ({src_layer}) → {tgt_file} ({tgt_layer})",
                    suggested_fix=f"Consider if this reference is necessary, or move the cited content into a layer-appropriate location.",
                    metadata={"src_layer": src_layer, "tgt_layer": tgt_layer},
                ))


import re
