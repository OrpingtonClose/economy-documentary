> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Vault Auditor Scaffold

A **multi-agent, knowledge-graph-powered** consistency auditor for the V7.1 architecture vault.

## Architecture

Inspired by three open-source projects:
- **agentralabs/codebase** — Semantic compilation into navigable typed graphs
- **wirelessr/codebase-analyzer-agent** — Multi-agent review cycles (auditor + reviewer)
- **Lum1104/Understand-Anything** — Knowledge graph with concept extraction and cross-references

## Components

### `vault_knowledge_graph.py`
Parses all 23 vault markdown files into a semantic graph:
- **Nodes**: files, sections, paragraphs, concepts (backtick-quoted terms)
- **Edges**: `contains`, `cites` (§NN refs, wikilinks), `mentions` (concepts), `precedes`
- **Context assembly**: For any paragraph, gathers same-file neighbors, cited sections, mentioned concepts, and graph-proximity related paragraphs

### `vault_auditor_agent.py`
DeepSeek v4-flash agent that audits ONE paragraph at a time:
- Receives: paragraph text + rich graph context + project summary
- Checks: naming consistency, cross-references, contradictions, undefined terms, missing references, principle violations
- Outputs: structured JSON finding with verdict, category, evidence, suggested fix, confidence, reasoning chain

### `vault_reviewer_agent.py`
Second DeepSeek v4-flash agent that reviews auditor findings:
- Checks for common false positive patterns (PascalCase vs snake_case, "poll" terminology, pseudocode conventions)
- Accepts or rejects findings with detailed feedback
- Triggers re-audit with reviewer feedback if rejected

### `vault_audit_orchestrator.py`
Central conductor implementing the nested loop:
1. **Outer loop**: For each of 522 paragraphs, build graph context
2. **Inner loop (Auditor)**: DeepSeek analyzes paragraph + context
3. **Inner loop (Reviewer)**: Second agent validates finding
4. **Feedback cycle**: If rejected, re-audit with enriched context (up to N times)
5. **Concurrency**: Processes up to 3 paragraphs in parallel via asyncio semaphore

## Usage

```bash
# Full audit (522 paragraphs, ~30-45 min with concurrency=3)
python3 vault_audit_orchestrator.py \
  --vault-dir /path/to/obsidian-vault \
  --api-key-file /path/to/key.txt \
  --concurrency 3 \
  --output audit_report.json

# Quick test on first 10 paragraphs
python3 vault_audit_orchestrator.py \
  --vault-dir /path/to/obsidian-vault \
  --limit 10 \
  --output quick_audit.json
```

## Output Format

```json
{
  "audit_run": {
    "started_at": "...",
    "finished_at": "...",
    "total_paragraphs": 522,
    "statistics": {
      "clean_rate_pct": 97.1,
      "verdict_distribution": {"CLEAN": 507, "MINOR": 5, "MODERATE": 7, "CRITICAL": 3}
    }
  },
  "findings": [
    {
      "paragraph_id": "03 - Effect Type Family Complete Schemas.md::para-10",
      "file": "03 - Effect Type Family Complete Schemas.md",
      "section": "3.3 Job Effects",
      "verdict": "MODERATE",
      "category": "missing",
      "description": "Provisioner retry override authority not documented in §10",
      "evidence": "...",
      "suggested_fix": "Add retry policy section to Provisioner Agent doc",
      "confidence": 0.85,
      "reasoning_chain": ["Step 1: ...", "Step 2: ..."],
      "review": {
        "accepted": true,
        "feedback": "...",
        "false_positive_reason": ""
      }
    }
  ]
}
```

## Key Design Decisions

- **No hardcoded concept lists**: The graph builder extracts ALL backtick-quoted terms as generic concepts. Semantic classification is performed by the auditor agent.
- **No timeouts**: Uses aiohttp with default (infinite) timeouts. If the API hangs, the operator intervenes.
- **No environment variables**: API key read from file path passed as CLI argument.
- **DeepSeek v4-flash only**: Both auditor and reviewer use deepseek-v4-flash for reasoning.
