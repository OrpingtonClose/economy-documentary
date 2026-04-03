"""
Multi-source search provider — ported from deep-search-portal.

Fan-out to multiple search engines concurrently, deduplicate by URL,
route by category.  Drop-in replacement for single-source SearXNG.

Providers:
  - DuckDuckGo (free, no key)
  - Brave Search (api key)
  - Tavily (ai-optimized)
  - Exa (neural/semantic)
  - Perplexity Sonar (grounded, citation-backed)

Category routing:
  "general"  → DDG + Brave + Tavily concurrent
  "news"     → DDG News + Tavily
  "economic" → FRED + Exa (financial domains) + DDG
  "academic" → Exa (academic domains) + DDG site:scholar
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, quote_plus

import httpx
from .llm import _get_client

log = logging.getLogger("enrichment")


def _read_key(filename: str) -> str:
    path = f"/Volumes/Shared/api_keys/{filename}"
    try:
        return open(path).read().strip()
    except FileNotFoundError:
        return os.getenv(filename.replace(".txt", "").upper(), "")


EXA_KEY = _read_key("exa_api_key.txt")
TAVILY_KEY = _read_key("tavily_api_key.txt")
BRAVE_KEY = _read_key("brave_search_api.txt") if os.path.exists("/Volumes/Shared/api_keys/brave_search_api.txt") else ""
GOOGLE_KEY = _read_key("google_search_api.txt")
GOOGLE_CX = _read_key("google_search_engine_id.txt")


# ── Data model ──────────────────────────────────────────────────

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str  # which provider returned this
    score: float = 0.0
    published_date: str = ""


# ── URL normalization for dedup ─────────────────────────────────

def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    url = re.sub(r'^https?://(www\.)?', '', url)
    return url.lower()


def _dedup_results(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    deduped = []
    for r in results:
        key = _normalize_url(r.url)
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped


# ── DuckDuckGo (free, no key) ──────────────────────────────────

async def _search_ddg(query: str, max_results: int = 10) -> list[SearchResult]:
    """DuckDuckGo via HTML scraping (no API key needed)."""
    try:
        from duckduckgo_search import DDGS
        ddgs = DDGS()
        raw = ddgs.text(query, max_results=max_results)
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("href", r.get("link", "")),
                snippet=r.get("body", r.get("snippet", ""))[:300],
                source="duckduckgo",
            )
            for r in raw if r.get("href") or r.get("link")
        ]
    except Exception as e:
        log.debug(f"DDG search error: {e}")
        return []


async def _search_ddg_news(query: str, max_results: int = 10) -> list[SearchResult]:
    """DuckDuckGo news search."""
    try:
        from duckduckgo_search import DDGS
        ddgs = DDGS()
        raw = ddgs.news(query, max_results=max_results)
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", r.get("link", "")),
                snippet=r.get("body", r.get("snippet", ""))[:300],
                source="duckduckgo_news",
                published_date=r.get("date", ""),
            )
            for r in raw if r.get("url") or r.get("link")
        ]
    except Exception as e:
        log.debug(f"DDG news error: {e}")
        return []


# ── Tavily (AI-optimized) ──────────────────────────────────────

async def _search_tavily(query: str, max_results: int = 5) -> list[SearchResult]:
    if not TAVILY_KEY:
        return []
    try:
        client = _get_client()
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_KEY,
                "query": query,
                "search_depth": "advanced",
                "max_results": max_results,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", "")[:300],
                source="tavily",
                score=r.get("score", 0.0),
            )
            for r in data.get("results", [])
        ]
    except Exception as e:
        log.debug(f"Tavily search error: {e}")
        return []


# ── Exa (neural/semantic) ──────────────────────────────────────

async def _search_exa(
    query: str,
    max_results: int = 5,
    include_domains: list[str] | None = None,
) -> list[SearchResult]:
    if not EXA_KEY:
        return []
    try:
        body: dict = {
            "query": query,
            "numResults": max_results,
            "type": "neural",
        }
        if include_domains:
            body["includeDomains"] = include_domains

        client = _get_client()
        resp = await client.post(
            "https://api.exa.ai/search",
            headers={"Authorization": f"Bearer {EXA_KEY}", "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("text", "")[:300],
                source="exa",
                score=r.get("score", 0.0),
                published_date=r.get("publishedDate", ""),
            )
            for r in data.get("results", [])
        ]
    except Exception as e:
        log.debug(f"Exa search error: {e}")
        return []


# ── Google Custom Search ────────────────────────────────────────

async def _search_google(query: str, max_results: int = 5) -> list[SearchResult]:
    if not GOOGLE_KEY or not GOOGLE_CX:
        return []
    try:
        client = _get_client()
        resp = await client.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": GOOGLE_KEY,
                "cx": GOOGLE_CX,
                "q": query,
                "num": min(max_results, 10),
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("link", ""),
                snippet=r.get("snippet", "")[:300],
                source="google",
            )
            for r in data.get("items", [])
        ]
    except Exception as e:
        log.debug(f"Google search error: {e}")
        return []


# ── Category-routed multi-search ───────────────────────────────

# Economic/financial domains for targeted Exa search
_ECONOMIC_DOMAINS = [
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "economist.com", "cnbc.com", "forbes.com", "marketwatch.com",
    "sec.gov", "eia.gov", "bls.gov", "fred.stlouisfed.org",
    "federalreserve.gov", "treasury.gov", "imf.org", "worldbank.org",
    "nber.org", "brookings.edu", "piie.com", "cbo.gov",
]

_ACADEMIC_DOMAINS = [
    "scholar.google.com", "semanticscholar.org", "researchgate.net",
    "arxiv.org", "ssrn.com", "nber.org", "jstor.org",
    "nature.com", "science.org", "sciencedirect.com",
]


async def multi_search(query: str, max_results: int = 15) -> list[SearchResult]:
    """General web search — fan out to all providers, deduplicate."""
    tasks = [
        _search_ddg(query, max_results=10),
        _search_tavily(query, max_results=5),
        _search_google(query, max_results=5),
    ]
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    all_results = []
    for r in results_lists:
        if isinstance(r, list):
            all_results.extend(r)

    return _dedup_results(all_results)[:max_results]


async def multi_search_news(query: str, max_results: int = 10) -> list[SearchResult]:
    """News-focused search."""
    tasks = [
        _search_ddg_news(query, max_results=10),
        _search_tavily(query + " latest news", max_results=5),
    ]
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    all_results = []
    for r in results_lists:
        if isinstance(r, list):
            all_results.extend(r)

    return _dedup_results(all_results)[:max_results]


async def multi_search_economic(query: str, max_results: int = 10) -> list[SearchResult]:
    """Economic/financial search — prioritize authoritative sources."""
    tasks = [
        _search_exa(query, max_results=5, include_domains=_ECONOMIC_DOMAINS),
        _search_ddg(query + " site:fred.stlouisfed.org OR site:bls.gov OR site:reuters.com", max_results=5),
        _search_tavily(query, max_results=5),
    ]
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    all_results = []
    for r in results_lists:
        if isinstance(r, list):
            all_results.extend(r)

    return _dedup_results(all_results)[:max_results]


async def multi_search_academic(query: str, max_results: int = 10) -> list[SearchResult]:
    """Academic/research search."""
    tasks = [
        _search_exa(query, max_results=5, include_domains=_ACADEMIC_DOMAINS),
        _search_ddg(query + " site:scholar.google.com OR site:arxiv.org OR site:nber.org", max_results=5),
    ]
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    all_results = []
    for r in results_lists:
        if isinstance(r, list):
            all_results.extend(r)

    return _dedup_results(all_results)[:max_results]


async def search(query: str, category: str = "general", max_results: int = 10) -> list[SearchResult]:
    """Main entry point — routes by category.

    Categories: general, news, economic, academic
    """
    routers = {
        "general": multi_search,
        "news": multi_search_news,
        "economic": multi_search_economic,
        "academic": multi_search_academic,
    }
    func = routers.get(category, multi_search)
    return await func(query, max_results=max_results)


def format_results(results: list[SearchResult]) -> str:
    """Format search results as text for LLM consumption."""
    if not results:
        return "No results found."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.title}")
        lines.append(f"   URL: {r.url}")
        if r.snippet:
            lines.append(f"   {r.snippet}")
        lines.append(f"   [via: {r.source}]")
        lines.append("")
    return "\n".join(lines)


def available_providers() -> list[str]:
    """Return list of configured search providers."""
    providers = ["duckduckgo"]  # Always available
    if TAVILY_KEY:
        providers.append("tavily")
    if EXA_KEY:
        providers.append("exa")
    if GOOGLE_KEY and GOOGLE_CX:
        providers.append("google")
    if BRAVE_KEY:
        providers.append("brave")
    return providers
