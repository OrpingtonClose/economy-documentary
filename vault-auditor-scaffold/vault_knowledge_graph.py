"""
Vault Knowledge Graph Builder

Inspired by:
- agentralabs/codebase: semantic compilation into typed nodes/edges
- Lum1104/Understand-Anything: knowledge graph with concept extraction
- wirelessr/codebase-analyzer-agent: multi-agent progressive analysis

Compiles the entire vault into a navigable semantic graph where:
- Every paragraph is a typed node with full metadata
- Cross-references (§NN, wikilinks, file links) are typed edges
- Concepts extracted by LLM are linked to paragraphs
- The graph supports neighborhood queries for context assembly
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ─── Graph Schema ────────────────────────────────────────────────────────────

@dataclass
class GraphNode:
    id: str
    type: str  # "file", "section", "paragraph", "concept", "effect", "agent"
    name: str
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdge:
    source: str
    target: str
    type: str  # "contains", "cites", "contradicts", "defines", "mentions", "precedes"
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


# ─── Vault Parser ────────────────────────────────────────────────────────────

class VaultParser:
    """Parse markdown vault files into structured paragraphs with metadata."""

    def __init__(self, vault_dir: str):
        self.vault_dir = Path(vault_dir)
        self.files: list[VaultFile] = []

    def parse_all(self) -> list[VaultFile]:
        md_files = sorted(self.vault_dir.glob("*.md"))
        for fpath in md_files:
            self.files.append(self._parse_file(fpath))
        return self.files

    def _parse_file(self, fpath: Path) -> VaultFile:
        text = fpath.read_text(encoding="utf-8")
        frontmatter, body = self._extract_frontmatter(text)
        paragraphs = self._split_paragraphs_v2(body, fpath.name)
        return VaultFile(
            path=str(fpath),
            filename=fpath.name,
            frontmatter=frontmatter,
            paragraphs=paragraphs,
        )

    def _extract_frontmatter(self, text: str) -> tuple[dict, str]:
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    fm = json.loads(parts[1].strip())
                except json.JSONDecodeError:
                    fm = {}
                return fm, parts[2].strip()
        return {}, text

    def _split_sections(self, text: str) -> list[VaultSection]:
        lines = text.split("\n")
        sections: list[VaultSection] = []
        current_lines: list[str] = []
        current_heading = "(untitled)"
        current_level = 0

        for line in lines:
            m = re.match(r'^(#{1,6})\s+(.+)$', line)
            if m:
                if current_lines:
                    sections.append(VaultSection(
                        heading=current_heading,
                        level=current_level,
                        text="\n".join(current_lines).strip(),
                    ))
                current_heading = m.group(2).strip()
                current_level = len(m.group(1))
                current_lines = []
            else:
                current_lines.append(line)

        if current_lines:
            sections.append(VaultSection(
                heading=current_heading,
                level=current_level,
                text="\n".join(current_lines).strip(),
            ))
        return sections

    def _split_paragraphs_v2(self, text: str, filename: str) -> list[VaultParagraph]:
        """Split file text into paragraphs matching the v3 auditor logic.
        
        - Splits on ## or ### headings
        - Splits on blank lines when buffer > 400 chars
        - Code blocks (```) are separate paragraphs
        - Minimum paragraph length: 40 chars
        """
        paragraphs: list[VaultParagraph] = []
        lines = text.split("\n")
        current: list[str] = []
        current_heading = "INTRO"
        in_code = False
        para_idx = 0

        def flush_current():
            nonlocal para_idx
            if current:
                text = "\n".join(current).strip()
                if len(text) > 40:
                    paragraphs.append(VaultParagraph(
                        id=f"{filename}::para-{para_idx}",
                        filename=filename,
                        section_heading=current_heading,
                        section_level=0,
                        text=text,
                        is_code=in_code,
                        index=para_idx,
                    ))
                    para_idx += 1
                current.clear()

        for line in lines:
            if line.strip().startswith("```"):
                if in_code:
                    current.append(line)
                    flush_current()
                    in_code = False
                else:
                    flush_current()
                    current = [line]
                    in_code = True
                continue

            if in_code:
                current.append(line)
                continue

            # Heading boundary (## or ###)
            if line.startswith("## ") or line.startswith("### "):
                flush_current()
                current = [line]
                current_heading = line.lstrip("#").strip()
                continue

            # Blank line with buffer > 400 chars -> split
            if line.strip() == "" and len("\n".join(current)) > 400:
                flush_current()
                continue

            current.append(line)

        flush_current()
        return paragraphs


@dataclass
class VaultSection:
    heading: str
    level: int
    text: str


@dataclass
class VaultParagraph:
    id: str
    filename: str
    section_heading: str
    section_level: int
    text: str
    is_code: bool
    index: int


@dataclass
class VaultFile:
    path: str
    filename: str
    frontmatter: dict
    paragraphs: list[VaultParagraph]


# ─── Knowledge Graph Builder ─────────────────────────────────────────────────

class VaultKnowledgeGraph:
    """
    Builds and queries a semantic knowledge graph from parsed vault files.

    Nodes: file, section, paragraph, concept, effect, agent, principle
    Edges: contains, cites, contradicts, defines, mentions, precedes, related
    """

    def __init__(self):
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self._adj_out: dict[str, list[GraphEdge]] = {}  # source -> edges
        self._adj_in: dict[str, list[GraphEdge]] = {}   # target -> edges

    def add_node(self, node: GraphNode) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: GraphEdge) -> None:
        self.edges.append(edge)
        self._adj_out.setdefault(edge.source, []).append(edge)
        self._adj_in.setdefault(edge.target, []).append(edge)

    def neighbors(self, node_id: str, edge_type: str | None = None) -> list[GraphNode]:
        """Get neighboring nodes (outgoing edges)."""
        edges = self._adj_out.get(node_id, [])
        if edge_type:
            edges = [e for e in edges if e.type == edge_type]
        return [self.nodes[e.target] for e in edges if e.target in self.nodes]

    def predecessors(self, node_id: str, edge_type: str | None = None) -> list[GraphNode]:
        """Get predecessor nodes (incoming edges)."""
        edges = self._adj_in.get(node_id, [])
        if edge_type:
            edges = [e for e in edges if e.type == edge_type]
        return [self.nodes[e.source] for e in edges if e.source in self.nodes]

    def related_paragraphs(self, para_id: str, max_hops: int = 2) -> list[GraphNode]:
        """Find paragraphs related within max_hops via any edge type."""
        visited = {para_id}
        frontier = [para_id]
        results = []
        for _ in range(max_hops):
            next_frontier = []
            for nid in frontier:
                for edge in self._adj_out.get(nid, []) + self._adj_in.get(nid, []):
                    other = edge.target if edge.source == nid else edge.source
                    if other not in visited:
                        visited.add(other)
                        next_frontier.append(other)
                        if other in self.nodes and self.nodes[other].type == "paragraph":
                            results.append(self.nodes[other])
            frontier = next_frontier
        return results

    def build_from_vault(self, files: list[VaultFile]) -> None:
        """Ingest all vault files into the knowledge graph."""
        # Phase 1: Create file and paragraph nodes
        for vf in files:
            file_id = f"file:{vf.filename}"
            self.add_node(GraphNode(
                id=file_id,
                type="file",
                name=vf.filename,
                content="",
                metadata=vf.frontmatter,
            ))

            prev_para_id: str | None = None
            for para in vf.paragraphs:
                self.add_node(GraphNode(
                    id=para.id,
                    type="paragraph",
                    name=f"{vf.filename} :: {para.section_heading}",
                    content=para.text,
                    metadata={
                        "filename": vf.filename,
                        "section": para.section_heading,
                        "level": para.section_level,
                        "is_code": para.is_code,
                        "index": para.index,
                    },
                ))
                # contains edge: file -> paragraph
                self.add_edge(GraphEdge(
                    source=file_id,
                    target=para.id,
                    type="contains",
                ))
                # precedes edge: paragraph -> paragraph
                if prev_para_id:
                    self.add_edge(GraphEdge(
                        source=prev_para_id,
                        target=para.id,
                        type="precedes",
                    ))
                prev_para_id = para.id

        # Phase 2: Extract cross-references and backtick concepts
        for vf in files:
            for para in vf.paragraphs:
                self._extract_references(para)
                self._extract_backtick_concepts(para)

    def _extract_references(self, para: VaultParagraph) -> None:
        """Extract §NN references, wikilinks, and file links."""
        text = para.text
        # §NN or §NN.N references
        for m in re.finditer(r'§\s*(\d+(?:\.\d+)*)', text):
            ref = m.group(1)
            # Find target file/section by section number
            target_id = self._resolve_section_ref(ref, para.filename)
            if target_id:
                self.add_edge(GraphEdge(
                    source=para.id,
                    target=target_id,
                    type="cites",
                    metadata={"ref": ref},
                ))

        # Wikilinks [[file|alias]]
        for m in re.finditer(r'\[\[(.*?)\]\]', text):
            link = m.group(1).split("|")[0].strip()
            target_id = self._resolve_wikilink(link)
            if target_id:
                self.add_edge(GraphEdge(
                    source=para.id,
                    target=target_id,
                    type="cites",
                ))

    def _extract_backtick_concepts(self, para: VaultParagraph) -> None:
        """Extract all backtick-quoted terms as generic concept nodes.
        
        No semantic classification at build time. The auditor agent
        performs all semantic analysis. We just build the structural graph.
        """
        text = para.text
        for m in re.finditer(r'`([A-Za-z_][A-Za-z0-9_]*)`', text):
            term = m.group(1)
            # Skip trivial type names and keywords
            if term.lower() in {"true", "false", "none", "null", "str", "int", "float", "bool", "list", "dict", "set", "get", "post", "self", "cls", "def", "class", "import", "from", "return", "if", "else", "elif", "for", "while", "try", "except", "finally", "with", "as", "pass", "break", "continue", "lambda", "yield", "async", "await"}:
                continue
            concept_id = f"concept:{term.lower()}"
            if concept_id not in self.nodes:
                self.add_node(GraphNode(
                    id=concept_id,
                    type="concept",
                    name=term,
                ))
            self.add_edge(GraphEdge(
                source=para.id,
                target=concept_id,
                type="mentions",
            ))

    def _resolve_section_ref(self, ref: str, source_file: str) -> str | None:
        """Try to resolve a §NN reference to a paragraph node."""
        # Find a paragraph in a file whose section heading starts with the ref
        for nid, node in self.nodes.items():
            if node.type == "paragraph" and node.metadata.get("section", "").startswith(ref):
                return nid
        # Fallback: find file with matching section number in frontmatter
        for nid, node in self.nodes.items():
            if node.type == "file":
                section = node.metadata.get("section", "")
                if section and section.startswith(ref.split(".")[0]):
                    return nid
        return None

    def _resolve_wikilink(self, link: str) -> str | None:
        """Resolve a wikilink to a file node."""
        for nid, node in self.nodes.items():
            if node.type == "file":
                if link in node.name or node.name.replace(".md", "") in link:
                    return nid
        return None

    def get_context_for_paragraph(self, para_id: str, max_chars: int = 4000) -> str:
        """Assemble rich context for a paragraph: related paras, cited sections, mentioned concepts."""
        lines = []
        para = self.nodes.get(para_id)
        if not para:
            return ""

        # 1. Same-file paragraphs (preceding and following)
        file_id = f"file:{para.metadata['filename']}"
        file_paras = self.neighbors(file_id, "contains")
        file_paras.sort(key=lambda n: n.metadata.get("index", 0))
        para_idx = para.metadata.get("index", 0)
        nearby = [p for p in file_paras if abs(p.metadata.get("index", 0) - para_idx) <= 3]
        if nearby:
            lines.append("=== SAME-FILE CONTEXT ===")
            for p in nearby:
                prefix = ">>> " if p.id == para_id else "    "
                lines.append(f"{prefix}[{p.metadata.get('section', '?')}] {p.content[:200]}...")
            lines.append("")

        # 2. Cited paragraphs/sections
        cited = self.neighbors(para_id, "cites")
        if cited:
            lines.append("=== CITED SECTIONS ===")
            for c in cited[:5]:
                lines.append(f"  [{c.name}] {c.content[:200]}...")
            lines.append("")

        # 3. Mentions (effects, agents)
        mentioned = self.neighbors(para_id, "mentions")
        if mentioned:
            lines.append("=== MENTIONED CONCEPTS ===")
            for m in mentioned:
                lines.append(f"  {m.type.upper()}: {m.name}")
            lines.append("")

        # 4. Related paragraphs via graph hops
        related = self.related_paragraphs(para_id, max_hops=2)
        if related:
            lines.append("=== RELATED PARAGRAPHS (graph proximity) ===")
            for r in related[:5]:
                lines.append(f"  [{r.metadata.get('filename', '?')} :: {r.metadata.get('section', '?')}] {r.content[:150]}...")
            lines.append("")

        context = "\n".join(lines)
        if len(context) > max_chars:
            context = context[:max_chars] + "\n...[truncated]"
        return context

    def get_project_summary(self, max_chars: int = 2000) -> str:
        """Produce a high-level project summary for the auditor."""
        files = [n for n in self.nodes.values() if n.type == "file"]
        effects = [n for n in self.nodes.values() if n.type == "effect"]
        agents = [n for n in self.nodes.values() if n.type == "agent"]

        lines = [
            f"VAULT OVERVIEW:",
            f"  Files: {len(files)}",
            f"  Paragraphs: {len([n for n in self.nodes.values() if n.type == 'paragraph'])}",
            f"  Effects defined: {len(effects)}",
            f"  Agents mentioned: {len(agents)}",
            "",
            "FILES:",
        ]
        for f in sorted(files, key=lambda n: n.name):
            title = f.metadata.get("title", f.name)
            sec = f.metadata.get("section", "")
            lines.append(f"  §{sec} {title} ({f.name})")

        lines.extend(["", "EFFECTS:", "  " + ", ".join(sorted(e.name for e in effects))])
        lines.extend(["", "AGENTS:", "  " + ", ".join(sorted(a.name for a in agents))])

        summary = "\n".join(lines)
        if len(summary) > max_chars:
            summary = summary[:max_chars] + "\n...[truncated]"
        return summary


# ─── Convenience Builder ─────────────────────────────────────────────────────

def build_knowledge_graph(vault_dir: str) -> VaultKnowledgeGraph:
    """Parse vault and build full knowledge graph."""
    parser = VaultParser(vault_dir)
    files = parser.parse_all()
    graph = VaultKnowledgeGraph()
    graph.build_from_vault(files)
    return graph
