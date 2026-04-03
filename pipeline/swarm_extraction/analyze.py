"""
Tree-Reactor Enrichment Pipeline
=================================
Deep claim verification using the full Miro-persistent architecture.

Every corner plowed. Every source checked. Every obstacle an error.

Pipeline:
  1. Comprehend  — map the full knowledge territory
  2. Extract     — pull verifiable claims from 42 scenes
  3. Tree-verify — concurrent subagent research with native tool calling,
                   AoT state contraction, saturation detection, serendipity
  4. Cross-check — contradiction detection, fabrication removal
  5. Entities    — extract entity/relationship graph
  6. Persist     — JSONL + entity graph + enrichment report

Tools: 39 from deep-search-portal + 6 local (perplexity_verify, fred_lookup,
       economic_search, news_search, wolfram_compute, web_read)
LLMs:  MiniMax M2.7 → DeepSeek V3 → Perplexity → Gemini
Search: DDG + Tavily + Exa + Google (concurrent fan-out)
Fetch:  httpx → Playwright → Selenium → Bright Data → Oxylabs → Wayback
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import (
    AtomicCondition,
    EnrichmentResult,
    QueryComprehension,
    ResearchNode,
    ReasoningStep,
    ToolTrace,
)
from .condition_store import ConditionStore, QuestionRegistry
from .scoring import trust_score_url, serendipity_score
from .llm import call_llm
from .tool_defs import NATIVE_TOOLS
from .tool_executor import execute_tool, execute_tools_parallel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("enrichment")

# ── Configuration ───────────────────────────────────────────────

TREE_MAX_CONCURRENT = int(os.getenv("TREE_MAX_CONCURRENT", "4"))
MAX_SUBAGENT_TURNS = int(os.getenv("MAX_SUBAGENT_TURNS", "12"))
ENRICHMENT_OUTPUT_DIR = Path(os.getenv("ENRICHMENT_OUTPUT_DIR", "./enrichment"))


# ── Phase 1: Query Comprehension ───────────────────────────────

_COMPREHENSION_PROMPT = """You are a research analyst. Deeply understand the knowledge territory of this documentary.

The documentary covers:
{summary}

Analyze and output ONLY valid JSON:
{{
  "entities": ["every entity, institution, person, policy, financial instrument, geopolitical actor mentioned or implied — be exhaustive, 30+"],
  "domains": ["every knowledge domain: macroeconomics, monetary policy, geopolitics, energy markets, housing, private credit, trade policy, warfare economics, central banking, wealth inequality, cryptocurrency, AI economics, insurance, employment — 20+"],
  "implicit_questions": ["questions the documentary raises but doesn't answer — what a viewer would want to verify independently — 10+"],
  "adjacent_territories": ["where the DEEP verification data lives: FRED series, BLS employment reports, SEC filings, Fed meeting minutes, oil futures data, CBO projections, IMF World Economic Outlook, NBER working papers, Congressional Budget Office reports, EIA petroleum data — 15+"],
  "relevance_keywords": ["40+ keywords: economic jargon, policy terms, financial instruments, institutional names, market indicators, geopolitical terms"],
  "deep_knowledge_targets": ["specific data sources: FRED series IDs (GDP, CPIAUCSL, FEDFUNDS, GFDEBTN, TOTALSL, UMCSENT), BLS report numbers, EIA petroleum reports, specific Fed speeches, named economists, specific court cases — 15+"],
  "semantic_summary": "one paragraph on what this documentary is REALLY arguing — the thesis",
  "intent_type": "informational",
  "core_need": "verify every factual claim in this documentary against authoritative primary sources, find what's true, what's exaggerated, what's missing"
}}"""


async def comprehend_documentary(scenes: list[dict], req_id: str) -> QueryComprehension:
    """Build deep comprehension of the documentary's knowledge territory."""
    summary_parts = []
    for s in scenes:
        title = s.get("title", "")
        v2 = s.get("voice_blocks", {}).get("V2", {})
        text = v2.get("text", "")[:400] if isinstance(v2, dict) else ""
        summary_parts.append(f"Scene {s.get('scene_num', '?')}: {title} — {text}")

    summary = "\n".join(summary_parts)
    prompt = _COMPREHENSION_PROMPT.replace("{summary}", summary[:12000])

    result = await call_llm(
        [{"role": "user", "content": prompt}],
        req_id, max_tokens=4096, temperature=0.3,
    )

    if "error" in result:
        log.warning(f"Comprehension failed: {result['error']}")
        return QueryComprehension(semantic_summary="Documentary about economic polycrisis in 2026")

    content = result.get("content", "").strip()
    if content.startswith("```"):
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)

    try:
        data = json.loads(content)
        return QueryComprehension(
            entities=data.get("entities", [])[:40],
            domains=data.get("domains", [])[:25],
            implicit_questions=data.get("implicit_questions", [])[:15],
            adjacent_territories=data.get("adjacent_territories", [])[:20],
            relevance_keywords=data.get("relevance_keywords", [])[:50],
            deep_knowledge_targets=data.get("deep_knowledge_targets", [])[:20],
            semantic_summary=data.get("semantic_summary", ""),
            intent_type=data.get("intent_type", "informational"),
            core_need=data.get("core_need", ""),
        )
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"Comprehension JSON parse error: {e}")
        return QueryComprehension(semantic_summary=summary[:500])


# ── Phase 2: Claim Extraction ──────────────────────────────────

def extract_claims_from_scenes(scenes: list[dict]) -> list[dict]:
    """Extract verifiable claims from scene voice blocks."""
    claims = []
    claim_id = 0

    for scene in scenes:
        scene_num = scene.get("scene_num", 0)
        for voice_key, voice_data in scene.get("voice_blocks", {}).items():
            if not isinstance(voice_data, dict):
                continue
            text = voice_data.get("text", "")
            role = voice_data.get("role", "")

            sentences = re.split(r'(?<=[.!?])\s+', text)
            for sent in sentences:
                sent = sent.strip()
                if len(sent) < 40 or len(sent.split()) < 8:
                    continue

                indicators = [
                    r'\$[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|trillion))?',
                    r'\d+\.?\d*\s*%',
                    r'\d{1,3}(?:\.\d)?\s*(?:million|billion|trillion)',
                    r'(?:increased|decreased|rose|fell|grew|declined|dropped|jumped).*?\d',
                    r'(?:Fed|Federal Reserve|GDP|inflation|deficit|debt|unemployment)',
                    r'(?:oil|gas|gold|silver|bitcoin|tariff|sanctions)',
                    r'(?:according to|data shows|research shows)',
                    r'20[12]\d',
                ]
                if not any(re.search(p, sent, re.I) for p in indicators):
                    continue

                pressure = 0.5
                if re.search(r'\$[\d,]+\s*(?:trillion|billion)', sent, re.I):
                    pressure += 0.25
                if re.search(r'(?:war|invasion|sanctions|iran|israel|hormuz)', sent, re.I):
                    pressure += 0.15
                if re.search(r'(?:fed|federal reserve|inflation|recession|GDP|deficit)', sent, re.I):
                    pressure += 0.1
                if re.search(r'(?:oil|gas|gold|energy)', sent, re.I):
                    pressure += 0.1
                if role == "Patient Explainer":
                    pressure += 0.05

                claims.append({
                    "id": f"c{claim_id:04d}",
                    "text": sent,
                    "scene_num": scene_num,
                    "scene_title": scene.get("title", ""),
                    "voice_role": role,
                    "pressure": min(pressure, 1.0),
                })
                claim_id += 1

    return claims


# ── Phase 3: Deep Verification Subagent ────────────────────────

_VERIFY_SYSTEM = """You are a deep research verification agent for a documentary about the 2026 economic polycrisis. Today: {date}

MISSION: Verify this claim by digging into EVERY available source. Leave no stone unturned. Use tools aggressively — you have 39 tools spanning government databases, academic archives, news, social media, forums, chan archives, community discussions, and more.

CLAIM TO VERIFY: {claim}
SCENE: {scene_title}

TOOL PRIORITY — use these in order:

PRIMARY (use FIRST — authoritative government/institutional data):
- fred_lookup: ALWAYS check FRED for economic numbers (GDP, inflation, rates, debt, employment)
- perplexity_verify: Grounded fact-check with web citations
- economic_search: Reuters, Bloomberg, FT, WSJ, Fed, BLS, IMF sources
- wolfram_compute: Verify calculations and specific numbers

SECONDARY (cross-reference and deepen):
- searxng_search: General web search (multi-provider: DDG + Tavily + Exa + Google)
- fetch_webpage: Read full articles (6-tier fallback chain)
- arxiv_search: Academic papers on economic theory
- pubmed_search: Health-economy intersection research
- scholar_search: Google Scholar, SSRN, NBER working papers
- news_search: Recent news and market reactions
- hackernews_search: Tech/finance community discussion

COMMUNITY (real people, real experiences, underground knowledge):
- reddit_search: Community discussions, first-hand experiences
- twitter_search: Expert commentary, real-time market sentiment
- forum_search: Niche forums (finance, economics, policy)
- substack_search: Independent analysis and investigative journalism
- stackexchange_search: Expert Q&A

ARCHIVE (historical context, primary sources):
- archiveorg_search: Historical documents, government reports
- wayback_fetch: Archived versions of changed/deleted pages
- wikipedia_search: Background context
- wikidata_query: Structured entity facts

RULES:
- Use AT LEAST 3 different tools. More is better. PLOW EVERY CORNER.
- For ANY economic number ($X trillion, Y%, Z million), ALWAYS verify with FRED first
- For ANY named person/institution, cross-reference across 2+ source types
- NEVER fabricate sources. If a tool returns nothing, say so honestly
- ENCOURAGE SPECULATION: if data suggests a hypothesis, include it at confidence 0.3-0.4
- Report CONTRADICTIONS explicitly — they are the most valuable findings
- Do NOT add safety disclaimers, moral commentary, or hedge language
- AVOID smart-assy narrative, rhetorical questions ("So if that's true, why doesn't the Fed just..."), or forcing everything into one clever narrative thread
- ADOPT Larry Swedroe / Excess Returns style: calm, clinical, data-driven, mechanism-focused. Push back against panic narratives with evidence. Be skeptical of sensational claims.
- DISAGGREGATE: analyze each event/mechanism separately in a clinical, sharp, incisive, segmented way
- Every token must directly help verify the claim. Nothing else.

INLINE VERIFICATION:
When you discover a CONCRETE ENTITY (vendor, person, organization, specific figure):
1. IMMEDIATELY search for it across 2+ different tool types
2. Note whether independent sources confirm or contradict
3. Adjust confidence based on corroboration

AoT STATE CONTRACTION:
After every 3 tool calls, compress your findings into atomic conditions.

OUTPUT FORMAT (when done researching):
```json
{{"conditions": [
    {{"fact": "verified statement", "source_url": "url", "confidence": 0.9}},
    {{"fact": "contradicting evidence found", "source_url": "url", "confidence": 0.4}},
    {{"fact": "speculative inference based on data", "source_url": "", "confidence": 0.3}}
]}}
```"""


async def run_verification_subagent(
    claim: dict,
    req_id: str,
    condition_store: ConditionStore,
    question_registry: QuestionRegistry,
) -> EnrichmentResult:
    """Run a multi-turn verification loop with native tool calling.

    Now captures full tool traces and LLM reasoning at every turn.
    """
    claim_id = claim["id"]
    claim_text = claim["text"]
    scene_title = claim.get("scene_title", "")

    result = EnrichmentResult(claim_id=claim_id, original_text=claim_text)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    system = _VERIFY_SYSTEM.format(
        date=today, claim=claim_text, scene_title=scene_title,
    )

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Verify this claim thoroughly: {claim_text}"},
    ]

    # Pass ALL 39 tools for native function calling
    tools = NATIVE_TOOLS

    used_queries: set[str] = set()
    known_facts: list[str] = []
    consecutive_errors = 0

    for turn in range(1, MAX_SUBAGENT_TURNS + 1):
        log.info(f"    [{claim_id}] Turn {turn}/{MAX_SUBAGENT_TURNS}")

        # Call LLM with native tool definitions
        llm_result = await call_llm(
            messages, req_id,
            max_tokens=4096, temperature=0.3,
            tools=tools,
        )

        if "error" in llm_result:
            consecutive_errors += 1
            err_msg = llm_result["error"]
            log.warning(f"    [{claim_id}] Turn {turn} error: {err_msg}")
            result.reasoning_trace.append(ReasoningStep(
                turn=turn, content=f"LLM ERROR: {err_msg}",
                action="error",
            ))
            if consecutive_errors >= 3:
                result.error = err_msg
                break
            messages.append({"role": "assistant", "content": llm_result.get("error", "")})
            messages.append({"role": "user", "content": "Error occurred. Try a different approach."})
            continue

        consecutive_errors = 0
        content = llm_result.get("content", "") or ""
        tool_calls = llm_result.get("tool_calls")

        # ── No tool calls: LLM is done researching ──
        if not tool_calls:
            conditions = _parse_conditions(content, claim_id)
            n_extracted = len(conditions)
            n_admitted = 0
            if conditions:
                admission_results = await condition_store.admit_batch(conditions)
                admitted = [ar.condition for ar in admission_results if ar.admitted and ar.condition]
                result.conditions.extend(admitted)
                known_facts.extend(c.fact for c in admitted)
                n_admitted = len(admitted)

            result.reasoning_trace.append(ReasoningStep(
                turn=turn, content=content[:2000],
                tool_calls_requested=0,
                conditions_extracted=n_extracted,
                conditions_admitted=n_admitted,
                action="final_extraction",
            ))
            result.turns_used = turn
            break

        # ── Record reasoning step ──
        result.reasoning_trace.append(ReasoningStep(
            turn=turn, content=content[:2000],
            tool_calls_requested=len(tool_calls),
            action="research",
        ))

        # ── Execute tool calls ──
        assistant_msg: dict = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        calls_to_run: list[tuple[str, str, dict]] = []
        for tc in tool_calls:
            tc_id = tc.get("id", f"call_{uuid.uuid4().hex[:8]}")
            func = tc.get("function", {})
            tool_name = func.get("name", "unknown")
            args_str = func.get("arguments", "{}")

            try:
                arguments = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                arguments = {}

            query_key = f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"
            if query_key in used_queries:
                messages.append({
                    "role": "tool", "tool_call_id": tc_id,
                    "content": "Duplicate call skipped. Try a different query or tool.",
                })
                result.tool_trace.append(ToolTrace(
                    turn=turn, tool_name=tool_name, arguments=arguments,
                    was_duplicate=True,
                ))
                continue

            used_queries.add(query_key)
            calls_to_run.append((tc_id, tool_name, arguments))

        if calls_to_run:
            tool_results = await execute_tools_parallel(calls_to_run)
            result.tool_calls_made += len(tool_results)

            for tc_id, tool_name, tool_result, duration in tool_results:
                log.info(f"    [{claim_id}] {tool_name} ({duration:.1f}s) → {len(tool_result)} chars")

                # Record full trace — find original args by tc_id
                orig_args = {}
                for _cid, _cname, _cargs in calls_to_run:
                    if _cid == tc_id:
                        orig_args = _cargs
                        break
                result.tool_trace.append(ToolTrace(
                    turn=turn,
                    tool_name=tool_name,
                    arguments=orig_args,
                    result_snippet=tool_result[:500],
                    result_length=len(tool_result),
                    duration_sec=duration,
                ))

                truncated = tool_result
                if len(tool_result) > 8000:
                    truncated = tool_result[:6000] + "\n[...truncated...]\n" + tool_result[-1500:]

                messages.append({
                    "role": "tool", "tool_call_id": tc_id,
                    "content": truncated,
                })

        # ── AoT State Contraction every 3 turns ──
        if turn > 0 and turn % 3 == 0 and turn < MAX_SUBAGENT_TURNS:
            contraction_msgs = messages + [
                {"role": "user", "content": _CONDITION_EXTRACTION_PROMPT}
            ]
            extract_result = await call_llm(
                contraction_msgs, req_id,
                max_tokens=2048, temperature=0.1,
            )
            if "error" not in extract_result:
                mid_conditions = _parse_conditions(
                    extract_result.get("content", ""), claim_id,
                )
                if mid_conditions:
                    admission_results = await condition_store.admit_batch(mid_conditions)
                    admitted = [ar.condition for ar in admission_results if ar.admitted and ar.condition]

                    # Saturation detection
                    new_facts = [c.fact for c in admitted]
                    if known_facts:
                        novel = sum(1 for nf in new_facts if all(
                            _jaccard_words(nf, kf) < 0.6 for kf in known_facts
                        ))
                        novelty = novel / max(len(new_facts), 1)
                    else:
                        novelty = 1.0

                    known_facts.extend(new_facts)
                    result.conditions.extend(admitted)

                    # Record contraction reasoning
                    result.reasoning_trace.append(ReasoningStep(
                        turn=turn,
                        content=extract_result.get("content", "")[:2000],
                        conditions_extracted=len(mid_conditions),
                        conditions_admitted=len(admitted),
                        novelty=novelty,
                        action="contraction",
                    ))

                    if novelty < 0.1 and len(known_facts) >= 5:
                        log.info(f"    [{claim_id}] Saturation detected (novelty={novelty:.2f}), stopping")
                        result.reasoning_trace.append(ReasoningStep(
                            turn=turn, content=f"Saturation: novelty={novelty:.2f}, {len(known_facts)} known facts",
                            action="saturation_stop",
                        ))
                        result.turns_used = turn
                        break

                    # Reset context with compressed state
                    conditions_text = "\n".join(c.to_text() for c in admitted)
                    messages = [
                        {"role": "system", "content": system},
                        {"role": "user", "content": (
                            f"Continue verifying: {claim_text}\n\n"
                            f"Findings so far:\n{conditions_text}\n\n"
                            f"Find NEW information not covered above. "
                            f"Try different tools, different angles, deeper details."
                        )},
                    ]
                    log.info(f"    [{claim_id}] AoT contraction: {len(admitted)} conditions, novelty={novelty:.2f}")

        result.turns_used = turn

    # Final extraction if we exhausted turns without explicit conditions
    if result.turns_used >= MAX_SUBAGENT_TURNS and not result.conditions:
        messages.append({"role": "user", "content": _CONDITION_EXTRACTION_PROMPT})
        final = await call_llm(messages, req_id, max_tokens=2048, temperature=0.1)
        if "error" not in final:
            conditions = _parse_conditions(final.get("content", ""), claim_id)
            if conditions:
                admission_results = await condition_store.admit_batch(conditions)
                admitted_final = [ar.condition for ar in admission_results if ar.admitted and ar.condition]
                result.conditions.extend(admitted_final)
                result.reasoning_trace.append(ReasoningStep(
                    turn=result.turns_used,
                    content=final.get("content", "")[:2000],
                    conditions_extracted=len(conditions),
                    conditions_admitted=len(admitted_final),
                    action="final_extraction",
                ))

    # Determine verification status
    if result.conditions:
        high_conf = [c for c in result.conditions if c.confidence >= 0.7]
        disputed = [c for c in result.conditions
                    if any(w in c.fact.lower() for w in ("disputed", "false", "incorrect", "contradicts"))]
        if disputed:
            result.verification_status = "disputed"
            result.confidence = 0.3
        elif high_conf:
            result.verification_status = "verified"
            result.confidence = max(c.confidence for c in high_conf)
        else:
            result.verification_status = "partial"
            result.confidence = sum(c.confidence for c in result.conditions) / len(result.conditions)
        result.sources = list(set(c.source_url for c in result.conditions if c.source_url))
    else:
        result.verification_status = "unverified"
        result.confidence = 0.0

    return result


def _jaccard_words(a: str, b: str) -> float:
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa | wb), 1)


_CONDITION_EXTRACTION_PROMPT = """Extract all key findings as atomic conditions.

Output ONLY a JSON object:
{"conditions": [
    {"fact": "clear factual statement", "source_url": "url", "confidence": 0.9},
    ...
]}

Rules:
- Each fact = single, clear, verifiable statement
- Confidence: 0.9 = multi-source verified, 0.7 = single authoritative source, 0.5 = partial, 0.3 = speculative
- Include source URLs where available
- 3-15 conditions
- Include CONTRADICTIONS as separate conditions with lower confidence
- Output ONLY valid JSON"""


def _parse_conditions(content: str, claim_id: str) -> list[AtomicCondition]:
    """Parse AtomicConditions from LLM output."""
    if not content:
        return []

    # Try direct JSON parse
    try:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
            cleaned = re.sub(r'\s*```$', '', cleaned)

        json_match = re.search(r'\{[^{}]*"conditions"\s*:\s*\[.*?\]\s*\}', cleaned, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
        else:
            data = json.loads(cleaned)

        return [
            AtomicCondition(
                fact=c.get("fact", ""),
                source_url=c.get("source_url", ""),
                confidence=float(c.get("confidence", 0.5)),
                claim_id=claim_id,
                angle="verification",
            )
            for c in data.get("conditions", [])
            if c.get("fact")
        ]
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback
    if len(content.strip()) > 50:
        return [AtomicCondition(
            fact=content.strip()[:500],
            claim_id=claim_id, angle="verification", confidence=0.3,
        )]
    return []


# ── Phase 4: Cross-Check ──────────────────────────────────────

_CROSSCHECK_PROMPT = """You are a citation verification agent. Check these findings for:
1. Claims that CONTRADICT each other — flag with specific indices
2. Claims with confidence too high/low given their source quality
3. Claims that fabricate entities (companies, people, data that don't exist)
4. Speculative claims that are reasonable — label but KEEP them

Output ONLY valid JSON:
{
  "verified": [{"fact_index": 0, "adjusted_confidence": 0.8, "reason": "confirmed by FRED data"}],
  "contradictions": [{"fact_index_1": 0, "fact_index_2": 3, "description": "X says A but Y says B"}],
  "speculative": [{"fact_index": 2, "reason": "reasonable inference, no direct source"}],
  "fabricated": [{"fact_index": 7, "reason": "entity does not exist in any source"}]
}

Findings:
"""


async def cross_check_conditions(conditions: list[AtomicCondition], req_id: str) -> list[AtomicCondition]:
    if len(conditions) < 3:
        return conditions

    text = "\n".join(
        f"{i}. {c.fact} [source: {c.source_url}, conf: {c.confidence:.1f}]"
        for i, c in enumerate(conditions)
    )
    result = await call_llm(
        [{"role": "system", "content": _CROSSCHECK_PROMPT},
         {"role": "user", "content": text}],
        req_id, max_tokens=2048, temperature=0.1,
    )

    if "error" in result:
        return conditions

    content = result.get("content", "").strip()
    if content.startswith("```"):
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)

    try:
        data = json.loads(content)

        for v in data.get("verified", []):
            idx = v.get("fact_index", -1)
            if 0 <= idx < len(conditions):
                conditions[idx].confidence = float(v.get("adjusted_confidence", conditions[idx].confidence))
                conditions[idx].verification_status = "verified"

        for c in data.get("contradictions", []):
            for key in ("fact_index_1", "fact_index_2"):
                idx = c.get(key, -1)
                if 0 <= idx < len(conditions):
                    conditions[idx].confidence = max(0.1, conditions[idx].confidence - 0.2)

        for sp in data.get("speculative", []):
            idx = sp.get("fact_index", -1)
            if 0 <= idx < len(conditions):
                conditions[idx].verification_status = "speculative"

        fabricated = set()
        for f in data.get("fabricated", []):
            idx = f.get("fact_index", -1)
            if 0 <= idx < len(conditions):
                fabricated.add(idx)

        if fabricated:
            conditions = [c for i, c in enumerate(conditions) if i not in fabricated]
            log.info(f"Cross-check removed {len(fabricated)} fabricated conditions")

    except (json.JSONDecodeError, ValueError):
        pass

    return conditions


# ── Phase 5: Entity Extraction ─────────────────────────────────

_ENTITY_PROMPT = """Extract entities and relationships from these research findings about the 2026 economic crisis.

Output ONLY valid JSON:
{
  "entities": [
    {"name": "entity name", "type": "person|institution|policy|market|indicator|event|country|financial_instrument"}
  ],
  "relationships": [
    {"entity1": "name1", "entity2": "name2", "type": "relationship description", "is_bridge": false}
  ]
}

Mark cross-domain relationships (e.g. geopolitics→economics) as "is_bridge": true.

Findings:
"""


async def extract_entities(conditions: list[AtomicCondition], req_id: str) -> tuple[list[dict], list[dict]]:
    if not conditions:
        return [], []

    text = "\n".join(f"- {c.fact}" for c in conditions[:40])
    result = await call_llm(
        [{"role": "system", "content": _ENTITY_PROMPT}, {"role": "user", "content": text}],
        req_id, max_tokens=2048, temperature=0.1,
    )

    if "error" in result:
        return [], []

    content = result.get("content", "").strip()
    if content.startswith("```"):
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)

    try:
        data = json.loads(content)
        return data.get("entities", []), data.get("relationships", [])
    except (json.JSONDecodeError, ValueError):
        return [], []


# ── Incremental JSONL Stream Writer ────────────────────────────

class ClaimStream:
    """Append-only JSONL stream + realtime Obsidian vault builder.

    Each line = one complete claim result. Written and flushed immediately.
    If a VaultBuilder is attached, the claim page and all indexes are
    rebuilt on every emit(), so Obsidian reflects progress in realtime.

    If the process dies at claim 250/283, the first 250 lines are on disk
    and 250 claim pages exist in the vault.
    """

    def __init__(self, path: Path, vault_builder=None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w", encoding="utf-8")
        self._count = 0
        self._vault = vault_builder  # optional VaultBuilder instance

    def _to_record(self, claim: dict, result: EnrichmentResult) -> dict:
        """Serialize claim + result into a flat dict (same as one JSONL line)."""
        return {
            "claim_id": claim["id"],
            "scene_num": claim["scene_num"],
            "scene_title": claim.get("scene_title", ""),
            "claim_text": claim["text"],
            "pressure": claim.get("pressure", 0.5),
            "voice_role": claim.get("voice_role", ""),
            "status": result.verification_status,
            "confidence": result.confidence,
            "error": result.error,
            "turns_used": result.turns_used,
            "tool_calls_made": result.tool_calls_made,
            "conditions": [
                {
                    "fact": c.fact,
                    "source_url": c.source_url,
                    "confidence": c.confidence,
                    "angle": c.angle,
                    "domain": c.domain,
                    "trust_score": c.trust_score,
                    "verification_status": c.verification_status,
                    "source_type": c.source_type,
                    "entities": c.entities,
                }
                for c in result.conditions
            ],
            "sources": result.sources,
            "tool_trace": [
                {
                    "turn": t.turn,
                    "tool_name": t.tool_name,
                    "arguments": t.arguments,
                    "result_snippet": t.result_snippet,
                    "result_length": t.result_length,
                    "duration_sec": t.duration_sec,
                    "was_duplicate": t.was_duplicate,
                    "error": t.error,
                }
                for t in result.tool_trace
            ],
            "reasoning_trace": [
                {
                    "turn": r.turn,
                    "content": r.content,
                    "tool_calls_requested": r.tool_calls_requested,
                    "conditions_extracted": r.conditions_extracted,
                    "conditions_admitted": r.conditions_admitted,
                    "novelty": r.novelty,
                    "action": r.action,
                }
                for r in result.reasoning_trace
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def emit(self, claim: dict, result: EnrichmentResult):
        """Write claim to JSONL and update Obsidian vault."""
        record = self._to_record(claim, result)

        # 1. JSONL stream
        line = json.dumps(record, default=str, ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()
        self._count += 1

        # 2. Realtime vault update
        if self._vault:
            try:
                self._vault.on_claim(claim, record)
            except Exception as e:
                log.warning(f"  [{claim['id']}] Vault write error (non-fatal): {e}")

        log.info(f"  [{claim['id']}] Streamed + vault updated (#{self._count})")

    def close(self):
        self._fh.close()


# ── Phase 6: Persist ───────────────────────────────────────────

def persist_results(
    claims: list[dict],
    enrichment_results: list[EnrichmentResult],
    conditions: list[AtomicCondition],
    entities: list[dict],
    relationships: list[dict],
    comprehension: QueryComprehension,
    condition_store: ConditionStore,
    output_dir: Path,
):
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()

    # Enriched claims summary
    enriched = []
    for claim, result in zip(claims, enrichment_results):
        enriched.append({
            "claim_id": claim["id"],
            "original_text": claim["text"],
            "scene_num": claim["scene_num"],
            "scene_title": claim.get("scene_title", ""),
            "pressure": claim["pressure"],
            "verification_status": result.verification_status,
            "confidence": result.confidence,
            "sources": result.sources,
            "conditions_found": len(result.conditions),
            "tool_calls": result.tool_calls_made,
            "turns_used": result.turns_used,
            "error": result.error,
        })

    with open(output_dir / "enriched_claims.json", "w") as f:
        json.dump({"enriched_at": now, "total": len(enriched), "claims": enriched}, f, indent=2)

    # Conditions JSONL
    with open(output_dir / "conditions.jsonl", "w") as f:
        for c in conditions:
            f.write(json.dumps(asdict(c), default=str) + "\n")

    # Entity graph
    with open(output_dir / "entity_graph.json", "w") as f:
        json.dump({"entities": entities, "relationships": relationships}, f, indent=2)

    # Comprehension map
    with open(output_dir / "comprehension.json", "w") as f:
        json.dump(asdict(comprehension), f, indent=2)

    # Admission stats
    with open(output_dir / "admission_stats.json", "w") as f:
        json.dump(condition_store.stats, f, indent=2)

    # Human-readable report
    report = _build_report(enriched, conditions, entities, relationships, now)
    with open(output_dir / "ENRICHMENT_REPORT.md", "w") as f:
        f.write(report)

    log.info(f"Results written to {output_dir}/")


def _build_report(enriched, conditions, entities, relationships, timestamp):
    verified = sum(1 for e in enriched if e["verification_status"] == "verified")
    partial = sum(1 for e in enriched if e["verification_status"] == "partial")
    disputed = sum(1 for e in enriched if e["verification_status"] == "disputed")
    unverified = sum(1 for e in enriched if e["verification_status"] == "unverified")
    total_tools = sum(e["tool_calls"] for e in enriched)

    lines = [
        "# Documentary Claim Enrichment Report",
        f"Generated: {timestamp}",
        "",
        "## Summary",
        f"- Claims processed: {len(enriched)}",
        f"- Verified: {verified} ✅",
        f"- Partial: {partial} ⚠️",
        f"- Disputed: {disputed} ❌",
        f"- Unverified: {unverified} ❓",
        f"- Conditions admitted: {len(conditions)}",
        f"- Entities discovered: {len(entities)}",
        f"- Relationships found: {len(relationships)}",
        f"- Total tool calls: {total_tools}",
        "",
        "## Results by Scene",
        "",
    ]

    by_scene: dict[int, list] = {}
    for e in enriched:
        by_scene.setdefault(e["scene_num"], []).append(e)

    for sn in sorted(by_scene.keys()):
        sc = by_scene[sn]
        title = sc[0].get("scene_title", f"Scene {sn}")
        lines.append(f"### Scene {sn}: {title}")
        for e in sc:
            icon = {"verified": "✅", "partial": "⚠️", "disputed": "❌", "unverified": "❓"}.get(
                e["verification_status"], "❓"
            )
            lines.append(f"  {icon} **[{e['verification_status']}]** {e['original_text'][:150]}")
            if e["sources"]:
                lines.append(f"     Sources: {', '.join(e['sources'][:3])}")
            lines.append(f"     (conf={e['confidence']:.2f}, {e['tool_calls']} tools, {e['turns_used']} turns)")
        lines.append("")

    return "\n".join(lines)


# ── Main Pipeline ──────────────────────────────────────────────

async def run_enrichment_pipeline(
    scenes_file: Path,
    output_dir: Path,
    max_claims: int = 50,
):
    req_id = f"enrich-{uuid.uuid4().hex[:8]}"
    start = time.monotonic()

    log.info("=" * 70)
    log.info("DOCUMENTARY CLAIM ENRICHMENT — DEEP VERIFICATION PIPELINE")
    log.info("=" * 70)
    log.info(f"Tools: {len(NATIVE_TOOLS)} (full deep-search-portal suite)")

    with open(scenes_file) as f:
        scenes = json.load(f)
    log.info(f"Loaded {len(scenes)} scenes")

    # Phase 1: Comprehend
    log.info("\n[Phase 1: Query Comprehension]")
    comprehension = await comprehend_documentary(scenes, req_id)
    log.info(f"  Entities: {len(comprehension.entities)}, Domains: {len(comprehension.domains)}")
    log.info(f"  Thesis: {comprehension.semantic_summary[:200]}...")

    # Phase 2: Extract
    log.info("\n[Phase 2: Claim Extraction]")
    all_claims = extract_claims_from_scenes(scenes)
    log.info(f"  {len(all_claims)} verifiable claims extracted")

    all_claims.sort(key=lambda c: c["pressure"], reverse=True)
    claims = all_claims[:max_claims]
    log.info(f"  Top {len(claims)} by pressure selected")

    # Initialize stores
    condition_store = ConditionStore(
        user_query="Verify all factual claims in this documentary",
        comprehension=comprehension,
    )
    question_registry = QuestionRegistry()

    # Phase 3: Deep verify — with incremental JSONL streaming + realtime vault
    log.info(f"\n[Phase 3: Deep Verification ({len(claims)} claims, {TREE_MAX_CONCURRENT} concurrent)]")
    sem = asyncio.Semaphore(TREE_MAX_CONCURRENT)

    # Initialize realtime vault builder
    vault_builder = None
    vault_output = Path(os.getenv(
        "VAULT_OUTPUT",
        "/Users/orpington/Documents/Obsidian Vault/Documentary Enrichment",
    ))
    try:
        # Import here to avoid circular deps at module level
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from build_vault import VaultBuilder, load_scenes as vault_load_scenes
        scenes_for_vault = vault_load_scenes(scenes_file)
        vault_builder = VaultBuilder(vault_output, scenes_for_vault)
        log.info(f"  Vault builder: {vault_output}")
    except Exception as e:
        log.warning(f"  Vault builder unavailable (non-fatal): {e}")

    # Initialize the claim stream — each claim is written as it completes
    stream = ClaimStream(output_dir / "claim_stream.jsonl", vault_builder=vault_builder)

    enrichment_results: list[EnrichmentResult] = []
    results_lock = asyncio.Lock()

    async def _verify_one(claim: dict) -> EnrichmentResult:
        async with sem:
            log.info(f"  [{claim['id']}] S{claim['scene_num']:02d} {claim['text'][:80]}...")
            r = await run_verification_subagent(claim, req_id, condition_store, question_registry)
            log.info(f"  [{claim['id']}] → {r.verification_status} (conf={r.confidence:.2f}, {r.tool_calls_made} tools, {r.turns_used} turns)")

            # ── INCREMENTAL WRITE: flush this claim to JSONL immediately ──
            async with results_lock:
                stream.emit(claim, r)
                enrichment_results.append(r)

            return r

    tasks = [_verify_one(c) for c in claims]
    await asyncio.gather(*tasks)

    stream.close()
    log.info(f"\n  Stream: {stream._count} claims written to {stream.path}")

    admitted = condition_store.conditions
    log.info(f"\n  Admission: {condition_store.stats}")

    # Phase 4: Cross-check
    log.info("\n[Phase 4: Cross-Check]")
    admitted = await cross_check_conditions(admitted, req_id)
    log.info(f"  {len(admitted)} conditions after cross-check")

    # Phase 5: Entities
    log.info("\n[Phase 5: Entity Extraction]")
    entities, relationships = await extract_entities(admitted, req_id)
    log.info(f"  {len(entities)} entities, {len(relationships)} relationships")

    # Phase 6: Persist
    log.info("\n[Phase 6: Persist]")
    persist_results(
        claims, enrichment_results, admitted,
        entities, relationships, comprehension,
        condition_store, output_dir,
    )

    elapsed = time.monotonic() - start
    v = sum(1 for r in enrichment_results if r.verification_status == "verified")
    p = sum(1 for r in enrichment_results if r.verification_status == "partial")
    d = sum(1 for r in enrichment_results if r.verification_status == "disputed")
    u = sum(1 for r in enrichment_results if r.verification_status == "unverified")
    tc = sum(r.tool_calls_made for r in enrichment_results)

    log.info("\n" + "=" * 70)
    log.info("ENRICHMENT COMPLETE")
    log.info("=" * 70)
    log.info(f"  Time: {elapsed:.1f}s ({elapsed/60:.1f}m)")
    log.info(f"  Claims: {len(claims)}  Verified: {v}  Partial: {p}  Disputed: {d}  Unverified: {u}")
    log.info(f"  Conditions: {len(admitted)}  Entities: {len(entities)}  Relationships: {len(relationships)}")
    log.info(f"  Tool calls: {tc}  Output: {output_dir}/")


# ── CLI ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Documentary Claim Enrichment Pipeline")
    parser.add_argument("--scenes", "-s", type=Path, default=Path("./data/scenes_parsed.json"))
    parser.add_argument("--output", "-o", type=Path, default=ENRICHMENT_OUTPUT_DIR)
    parser.add_argument("--max-claims", "-n", type=int, default=50)
    parser.add_argument("--concurrent", "-c", type=int, default=TREE_MAX_CONCURRENT)
    args = parser.parse_args()

    import pipeline.swarm_extraction.analyze as _self
    _self.TREE_MAX_CONCURRENT = args.concurrent

    asyncio.run(run_enrichment_pipeline(
        scenes_file=args.scenes,
        output_dir=args.output,
        max_claims=args.max_claims,
    ))


if __name__ == "__main__":
    main()
