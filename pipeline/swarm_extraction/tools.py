"""
Research tools for the enrichment subagents.

Each tool is an async function that takes a query/params and returns
a string result.  The subagent loop calls these based on LLM tool_calls.

Tools:
  - perplexity_verify: Fact-check a claim with grounded citations
  - fred_lookup: Query FRED (Federal Reserve Economic Data) for specific series
  - exa_search: Semantic search across quality financial/economic domains
  - tavily_search: AI-optimized web search for recent data
  - wolfram_compute: Computational verification of numbers
  - web_read: Extract content from a URL
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import httpx
from .llm import _get_client

log = logging.getLogger("enrichment")


def _read_key(filename: str) -> str:
    path = f"/Volumes/Shared/api_keys/{filename}"
    try:
        return open(path).read().strip()
    except FileNotFoundError:
        return os.getenv(filename.replace(".txt", "").upper(), "")


PERPLEXITY_KEY = _read_key("perplexity_api_key.txt")
EXA_KEY = _read_key("exa_api_key.txt")
TAVILY_KEY = _read_key("tavily_api_key.txt")
FRED_KEY = _read_key("fred_api_key.txt")
WOLFRAM_KEY = _read_key("wolfram_alpha_api.txt")
JINA_KEY = _read_key("jina_api_key.txt")
BRAVE_KEY = _read_key("brave_search_api_key.txt") if os.path.exists("/Volumes/Shared/api_keys/brave_search_api_key.txt") else ""

# ── Transcript corpus (loaded once, searched per-claim) ───────
TRANSCRIPT_DIR = Path("/tmp/economy-documentary/corpus/transcripts")
_transcript_index: list[dict] = []  # [{video_id, channel, title, text, words_set}]
_transcript_loaded = False


def _load_transcripts():
    global _transcript_index, _transcript_loaded
    if _transcript_loaded:
        return
    import duckdb
    catalog_path = Path("/tmp/economy-documentary/corpus/catalog.parquet")
    # Build video_id -> metadata map from catalog
    vid_meta = {}
    if catalog_path.exists():
        try:
            con = duckdb.connect()
            rows = con.sql(f"SELECT video_id, channel_name, title FROM read_parquet('{catalog_path}')").fetchall()
            vid_meta = {r[0]: {"channel": r[1], "title": r[2]} for r in rows}
            con.close()
        except Exception:
            pass

    if TRANSCRIPT_DIR.exists():
        for f in sorted(TRANSCRIPT_DIR.glob("*.json")):
            try:
                with open(f) as fh:
                    data = json.load(fh)
                vid = data.get("video_id", f.stem)
                text = data.get("full_text", "")
                if not text or len(text) < 100:
                    continue
                meta = vid_meta.get(vid, {})
                _transcript_index.append({
                    "video_id": vid,
                    "channel": meta.get("channel", ""),
                    "title": meta.get("title", ""),
                    "text": text,
                    "words_set": set(text.lower().split()),
                })
            except Exception:
                continue
    _transcript_loaded = True
    log.info(f"Transcript corpus: {len(_transcript_index)} videos, {sum(len(t['text']) for t in _transcript_index):,} chars")


# ── Tool definitions (OpenAI function-calling format) ───────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "perplexity_verify",
            "description": "Fact-check a specific claim using web search with citations. Returns a grounded answer with source URLs. Use for verifying factual assertions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string", "description": "The exact claim to verify"},
                },
                "required": ["claim"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fred_lookup",
            "description": "Look up economic data from FRED (Federal Reserve Economic Data). Use for GDP, inflation, interest rates, employment, debt figures, consumer data. Returns recent observations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "series_id": {"type": "string", "description": "FRED series ID (e.g. GDP, CPIAUCSL, FEDFUNDS, GFDEBTN, UNRATE, TOTALSL)"},
                    "search_text": {"type": "string", "description": "Free-text search if you don't know the series ID"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Multi-provider web search (DDG + Tavily + Google + Exa). Use for general research queries. Set category='economic' for financial sources, 'news' for recent events, 'academic' for papers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "category": {"type": "string", "enum": ["general", "news", "economic", "academic"], "description": "Search category (default: general)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "economic_search",
            "description": "Search authoritative economic/financial sources: Reuters, Bloomberg, FT, WSJ, Fed, BLS, IMF, World Bank, NBER, Brookings. Use for verifying economic claims against primary sources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Economic/financial search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "news_search",
            "description": "Search recent news articles. Use for current events, policy changes, market reactions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "News search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wolfram_compute",
            "description": "Computational verification using Wolfram Alpha. Use for checking specific numbers, calculations, unit conversions, and mathematical claims.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Computational query (e.g. 'US national debt 2026', 'compound interest 5% on 40 trillion')"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_read",
            "description": "Extract and read content from a specific URL. Use after finding a relevant source to read its full content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to read"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transcript_search",
            "description": "Search the documentary's source corpus — 548 YouTube transcripts (16M chars) from 37 financial channels. Use to check what analysts ACTUALLY SAID versus what the documentary claims they said. Returns matching excerpts with channel/video attribution. ALWAYS use this when a claim attributes a statement to a specific person or channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query — person name, topic, or quote fragment"},
                    "max_results": {"type": "integer", "description": "Max transcripts to return (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
]


# ── Tool implementations ────────────────────────────────────────

async def perplexity_verify(claim: str) -> str:
    """Fact-check a claim using Perplexity Sonar Pro."""
    try:
        client = _get_client()
        resp = await client.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {PERPLEXITY_KEY}"},
            json={
                "model": "sonar-pro",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a fact-checker. Verify the claim using current data. "
                            "Provide: 1) VERIFIED / PARTIALLY TRUE / DISPUTED / UNVERIFIABLE, "
                            "2) The correct figures if different, 3) Key sources. Be concise."
                        ),
                    },
                    {"role": "user", "content": f"Verify: {claim}"},
                ],
                "max_tokens": 1000,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        citations = data.get("citations", [])
        result = content
        if citations:
            result += "\n\nSources:\n" + "\n".join(f"- {c}" for c in citations[:5])
        return result
    except Exception as e:
        return f"[TOOL_ERROR] perplexity_verify: {e}"


async def fred_lookup(series_id: str = "", search_text: str = "") -> str:
    """Query FRED for economic data series."""
    try:
        client = _get_client()
        if search_text and not series_id:
            # Search for series
            resp = await client.get(
                "https://api.stlouisfed.org/fred/series/search",
                params={
                    "api_key": FRED_KEY,
                    "search_text": search_text,
                    "file_type": "json",
                    "limit": 5,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            series_list = data.get("seriess", [])
            if not series_list:
                return f"No FRED series found for: {search_text}"
            results = []
            for s in series_list[:5]:
                results.append(f"  {s['id']}: {s['title']} ({s.get('frequency_short', '?')}, last updated {s.get('last_updated', '?')})")
            return "FRED series matching '{}':\n{}".format(search_text, "\n".join(results))

        # Fetch observations for specific series
        resp = await client.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "api_key": FRED_KEY,
                "series_id": series_id,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 12,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        obs = data.get("observations", [])
        if not obs:
            return f"No observations found for FRED series: {series_id}"

        # Also fetch series metadata
        meta_resp = await client.get(
            "https://api.stlouisfed.org/fred/series",
            params={
                "api_key": FRED_KEY,
                "series_id": series_id,
                "file_type": "json",
            },
        )
        meta = {}
        if meta_resp.status_code == 200:
            meta_data = meta_resp.json()
            series_list = meta_data.get("seriess", [])
            if series_list:
                meta = series_list[0]

        header = f"FRED: {meta.get('title', series_id)}"
        if meta.get("units"):
            header += f" ({meta['units']})"
        header += f"\nFrequency: {meta.get('frequency', '?')}"
        header += f"\nSource: {meta.get('source', 'Federal Reserve Economic Data')}"
        header += f"\nURL: https://fred.stlouisfed.org/series/{series_id}\n"

        lines = [header, "Recent observations:"]
        for o in obs[:12]:
            lines.append(f"  {o['date']}: {o['value']}")
        return "\n".join(lines)

    except Exception as e:
        return f"[TOOL_ERROR] fred_lookup: {e}"


async def web_search(query: str, category: str = "general") -> str:
    """Multi-provider web search — fans out to DDG + Tavily + Google + Exa.

    Categories: general, news, economic, academic
    """
    try:
        from .search_providers import search, format_results
        results = await search(query, category=category, max_results=10)
        return format_results(results) if results else f"No results for: {query}"
    except Exception as e:
        return f"[TOOL_ERROR] web_search: {e}"


async def economic_search(query: str) -> str:
    """Search across authoritative economic/financial sources.

    Uses Exa (neural) targeting: Reuters, Bloomberg, FT, WSJ, Fed, BLS, IMF, etc.
    Plus DDG and Tavily as backup.
    """
    try:
        from .search_providers import multi_search_economic, format_results
        results = await multi_search_economic(query, max_results=10)
        return format_results(results) if results else f"No economic results for: {query}"
    except Exception as e:
        return f"[TOOL_ERROR] economic_search: {e}"


async def news_search(query: str) -> str:
    """Search for recent news across DDG News + Tavily."""
    try:
        from .search_providers import multi_search_news, format_results
        results = await multi_search_news(query, max_results=10)
        return format_results(results) if results else f"No news results for: {query}"
    except Exception as e:
        return f"[TOOL_ERROR] news_search: {e}"


async def wolfram_compute(query: str) -> str:
    """Query Wolfram Alpha Short Answers API."""
    try:
        client = _get_client()
        resp = await client.get(
            "https://api.wolframalpha.com/v2/query",
            params={
                "appid": WOLFRAM_KEY,
                "input": query,
                "format": "plaintext",
                "output": "json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("queryresult", {})
        if not result.get("success"):
            return f"Wolfram Alpha could not compute: {query}"

        lines = [f"Wolfram Alpha: {query}\n"]
        for pod in result.get("pods", []):
            title = pod.get("title", "")
            for sub in pod.get("subpods", []):
                text = sub.get("plaintext", "")
                if text:
                    lines.append(f"[{title}] {text}")
        return "\n".join(lines)

    except Exception as e:
        return f"[TOOL_ERROR] wolfram_compute: {e}"


async def web_read(url: str) -> str:
    """Read content from a URL using Jina Reader or direct fetch."""
    try:
        # Try Jina Reader first (better extraction)
        if JINA_KEY:
            client = _get_client()
            resp = await client.get(
                f"https://r.jina.ai/{url}",
                headers={"Authorization": f"Bearer {JINA_KEY}"},
            )
            if resp.status_code == 200:
                text = resp.text[:15000]
                return f"Content from {url}:\n{text}"

        # Fallback: direct fetch with html stripping
        client = _get_client()
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DocEnrichBot/1.0)"},
        )
        resp.raise_for_status()
        text = resp.text
        # Basic HTML stripping
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return f"Content from {url}:\n{text[:15000]}"

    except Exception as e:
        return f"[TOOL_ERROR] web_read({url}): {e}"


async def transcript_search(query: str, max_results: int = 5) -> str:
    """Search the 548-video transcript corpus for matching content."""
    _load_transcripts()
    if not _transcript_index:
        return "[TOOL_ERROR] Transcript corpus not available"

    query_words = set(query.lower().split())
    if not query_words:
        return "Empty query"

    # Score by word overlap (Jaccard-like) + substring match bonus
    scored = []
    query_lower = query.lower()
    for t in _transcript_index:
        overlap = len(query_words & t["words_set"])
        if overlap < 2:
            continue
        jaccard = overlap / max(len(query_words | t["words_set"]), 1)
        # Bonus for substring match (exact phrases)
        substr_bonus = 0.5 if query_lower in t["text"].lower() else 0.0
        scored.append((jaccard + substr_bonus, t))

    scored.sort(key=lambda x: -x[0])

    if not scored:
        return f"No transcripts matched: {query}"

    results = []
    for score, t in scored[:max_results]:
        # Find the best matching excerpt (window around first match)
        text_lower = t["text"].lower()
        idx = text_lower.find(query_lower)
        if idx == -1:
            # Find any query word
            for w in sorted(query_words, key=len, reverse=True):
                idx = text_lower.find(w)
                if idx >= 0:
                    break
        if idx < 0:
            idx = 0

        # Extract window
        start = max(0, idx - 200)
        end = min(len(t["text"]), idx + 800)
        excerpt = t["text"][start:end]
        if start > 0:
            excerpt = "..." + excerpt
        if end < len(t["text"]):
            excerpt += "..."

        results.append(
            f"[{t['channel']}] \"{t['title']}\" (video:{t['video_id']}, score:{score:.3f})\n"
            f"{excerpt}\n"
        )

    header = f"Transcript search: {len(scored)} matches for \"{query}\", showing top {min(max_results, len(scored))}:\n\n"
    return header + "\n---\n".join(results)


# ── Tool executor ───────────────────────────────────────────────

TOOL_DISPATCH = {
    "perplexity_verify": perplexity_verify,
    "fred_lookup": fred_lookup,
    "web_search": web_search,
    "economic_search": economic_search,
    "news_search": news_search,
    "wolfram_compute": wolfram_compute,
    "web_read": web_read,
    "transcript_search": transcript_search,
}


async def execute_tool(name: str, arguments: dict) -> str:
    """Execute a named tool with arguments.  Returns result string."""
    func = TOOL_DISPATCH.get(name)
    if not func:
        return f"[TOOL_ERROR] Unknown tool: {name}"

    try:
        return await func(**arguments)
    except TypeError as e:
        return f"[TOOL_ERROR] Bad arguments for {name}: {e}"
    except Exception as e:
        return f"[TOOL_ERROR] {name} failed: {e}"


async def execute_tools_parallel(
    calls: list[tuple[str, str, dict]],
) -> list[tuple[str, str, str, float]]:
    """Execute multiple tool calls concurrently.

    Input:  [(call_id, tool_name, arguments), ...]
    Output: [(call_id, tool_name, result_string, duration_seconds), ...]
    """
    async def _run_one(call_id: str, name: str, args: dict):
        t0 = asyncio.get_event_loop().time()
        result = await execute_tool(name, args)
        dt = asyncio.get_event_loop().time() - t0
        return (call_id, name, result, dt)

    tasks = [_run_one(cid, name, args) for cid, name, args in calls]
    return await asyncio.gather(*tasks)
