"""
Additional search tools — ported from deep-search-portal/proxies/tools/search_tools2.py.

Specialized tools: Twitter/X (3-tier), Python exec, arXiv, Wayback Machine,
Wikidata, Hacker News, Stack Exchange, PubMed, Wikipedia, Archive.org,
forums, Google Scholar, Substack, Telegram, darknet market OSINT,
Facebook, Discord, Signal, WhatsApp, Crunchbase, Trustpilot, WHOIS,
YouTube search + transcript + metadata + video analyze (stubbed).

Differences from original:
  - API keys read from /Volumes/Shared/api_keys/ via _read_key()
  - asyncio.Semaphore throttling instead of get_throttler()
  - SearXNG calls replaced with search_providers.multi_search + site: operators
  - Social media via Bright Data / Oxylabs proxy APIs directly
  - YouTube video analysis (Qwen Omni) stubbed as unavailable
  - WhisperX kept as optional try/except import
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import httpx
from .llm import _get_client

from .search_providers import multi_search, multi_search_news, format_results, SearchResult
from .scoring import trust_score_url

log = logging.getLogger("enrichment")


# ── API key helpers ──────────────────────────────────────────────

def _read_key(filename: str) -> str:
    path = f"/Volumes/Shared/api_keys/{filename}"
    try:
        return open(path).read().strip()
    except FileNotFoundError:
        return os.getenv(filename.replace(".txt", "").upper(), "")


BRIGHT_DATA_API_KEY = _read_key("brightdata_key.txt")
BRIGHT_DATA_CUSTOMER_ID = "hl_dc044bf4"
BRIGHT_DATA_ZONE = "mcp_unlocker"

# Oxylabs — try multiple filenames
OXYLABS_CREDS = _read_key("oxylab_api.txt") or _read_key("oxylabs_residential_proxy.txt")
# Expect "user:pass" format
if ":" in OXYLABS_CREDS:
    OXYLABS_USERNAME, OXYLABS_PASSWORD = OXYLABS_CREDS.split(":", 1)
else:
    OXYLABS_USERNAME = OXYLABS_CREDS
    OXYLABS_PASSWORD = ""

APIFY_KEY = _read_key("apify_key.txt")


# ── Constants ────────────────────────────────────────────────────

WEBPAGE_MAX_CHARS = 30000
PYTHON_TIMEOUT = 30
PYTHON_OUTPUT_MAX = 10000


# ── Throttle semaphores ──────────────────────────────────────────

_semaphores: dict[str, asyncio.Semaphore] = {}


def _get_semaphore(name: str, limit: int = 3) -> asyncio.Semaphore:
    if name not in _semaphores:
        _semaphores[name] = asyncio.Semaphore(limit)
    return _semaphores[name]


# ── HTTP client factory ──────────────────────────────────────────

_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=10),
        )
    return _http_client


# ── HTML helpers ─────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _is_censored_response(text: str) -> bool:
    """Check for common censored/blocked page patterns."""
    lowered = text.lower()
    markers = [
        "access denied", "please verify you are a human",
        "enable javascript", "captcha", "rate limit",
        "403 forbidden", "just a moment",
    ]
    return any(m in lowered for m in markers)


# ── Format search results (local version) ───────────────────────

def _format_search_results(results: list[dict], source_label: str = "") -> str:
    """Format search result dicts into readable text."""
    if not results:
        return ""

    formatted = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        url = r.get("url", "")
        snippet = r.get("content", r.get("snippet", ""))[:300]
        trust = trust_score_url(url)
        tag = f" ({source_label})" if source_label else ""
        formatted.append(
            f"{i}. **{title}** [trust: {trust:.1f}]{tag}\n"
            f"   URL: {url}\n   {snippet}"
        )

    return "\n\n".join(formatted)


def _format_search_result_objects(results: list[SearchResult], source_label: str = "") -> str:
    """Format SearchResult objects into readable text."""
    if not results:
        return ""

    formatted = []
    for i, r in enumerate(results, 1):
        trust = trust_score_url(r.url)
        tag = f" ({source_label})" if source_label else ""
        formatted.append(
            f"{i}. **{r.title}** [trust: {trust:.1f}]{tag}\n"
            f"   URL: {r.url}\n   {r.snippet[:300]}"
        )

    return "\n\n".join(formatted)


# ── Web page fetching ────────────────────────────────────────────

async def _fetch_webpage(url: str) -> str:
    """Fetch and extract text from a webpage."""
    try:
        client = _get_http_client()
        resp = await client.get(
            url,
            timeout=20.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        )
        if resp.status_code != 200:
            return f"[Fetch error: HTTP {resp.status_code}]"
        text = _strip_html(resp.text)
        if len(text) > WEBPAGE_MAX_CHARS:
            text = text[:WEBPAGE_MAX_CHARS] + "\n[... truncated ...]"
        return text
    except Exception as e:
        return f"[Fetch error: {e}]"


# ============================================================================
# Twitter/X Search Tool (3-tier: Bright Data → Oxylabs → Nitter)
# ============================================================================

async def tool_twitter_search(query: str) -> str:
    """Search Twitter/X for tweets matching the query.

    Uses a tiered approach:
      1. Bright Data Twitter Scraper (if configured) — most reliable
      2. Oxylabs Web Scraper (if configured) — fallback
      3. Nitter instances (degraded, sporadic) — last resort

    Accepts Twitter search operators: from:handle, since:YYYY-MM-DD,
    until:YYYY-MM-DD, "exact phrase", etc.
    """
    # Tier 1: Bright Data Web Unlocker for Twitter search
    if BRIGHT_DATA_API_KEY:
        result = await _twitter_via_bright_data(query)
        if result:
            return result

    # Tier 2: Oxylabs for Twitter search
    if OXYLABS_USERNAME:
        result = await _twitter_via_oxylabs(query)
        if result:
            return result

    # Tier 3: Nitter instances (degraded fallback)
    result = await _twitter_via_nitter(query)
    if result:
        return result

    return (
        f"[TOOL_ERROR] Twitter search failed for: {query}. "
        "All access tiers exhausted (Bright Data, Oxylabs, Nitter). "
        "This is a technical failure, NOT 'no results found'. "
        "Twitter requires commercial proxy access for reliable results."
    )


async def _twitter_via_bright_data(query: str) -> Optional[str]:
    """Scrape Twitter search results via Bright Data Web Unlocker."""
    try:
        encoded_query = quote(query, safe="")
        search_url = f"https://x.com/search?q={encoded_query}&src=typed_query&f=live"
        proxy_url = (
            f"https://brd-customer-{BRIGHT_DATA_CUSTOMER_ID}-zone-{BRIGHT_DATA_ZONE}"
            f":{BRIGHT_DATA_API_KEY}@brd.superproxy.io:33335"
        )
        async with _get_semaphore("bright_data", 2):
            async with httpx.AsyncClient(
                proxy=proxy_url,
                verify=False,
                timeout=httpx.Timeout(45.0, connect=15.0),
                follow_redirects=True,
            ) as client:
                resp = await client.get(
                    search_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                if resp.status_code != 200:
                    return None

                text = _strip_html(resp.text)
                if not text or len(text.strip()) < 100:
                    return None
                if _is_censored_response(text):
                    return None

                return f"**Twitter/X search results for: {query}**\n\n{text[:WEBPAGE_MAX_CHARS]}"
    except Exception as e:
        log.debug(f"Bright Data Twitter fetch failed: {e}")
        return None


async def _twitter_via_oxylabs(query: str) -> Optional[str]:
    """Scrape Twitter search results via Oxylabs Web Scraper."""
    if not OXYLABS_USERNAME or not OXYLABS_PASSWORD:
        return None
    try:
        encoded_query = quote(query, safe="")
        search_url = f"https://x.com/search?q={encoded_query}&src=typed_query&f=live"
        async with _get_semaphore("oxylabs", 2):
            async with httpx.AsyncClient(
                proxy=f"https://{OXYLABS_USERNAME}:{OXYLABS_PASSWORD}@unblock.oxylabs.io:60000",
                verify=False,
                timeout=httpx.Timeout(45.0, connect=15.0),
                follow_redirects=True,
            ) as client:
                resp = await client.get(
                    search_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                        ),
                    },
                )
                if resp.status_code != 200:
                    return None

                text = _strip_html(resp.text)
                if not text or len(text.strip()) < 100:
                    return None
                if _is_censored_response(text):
                    return None

                return f"**Twitter/X search results for: {query}**\n\n{text[:WEBPAGE_MAX_CHARS]}"
    except Exception as e:
        log.debug(f"Oxylabs Twitter fetch failed: {e}")
        return None


_NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.woodland.cafe",
]


async def _twitter_via_nitter(query: str) -> Optional[str]:
    """Search Twitter via Nitter instances (degraded, sporadic availability)."""
    client = _get_http_client()
    for instance in _NITTER_INSTANCES:
        try:
            async with _get_semaphore("nitter", 2):
                resp = await client.get(
                    f"{instance}/search",
                    params={"f": "tweets", "q": query},
                    timeout=15.0,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                        ),
                    },
                )
            if resp.status_code != 200:
                continue

            raw = resp.text

            # Parse Nitter HTML for tweet content
            tweet_blocks = re.findall(
                r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>',
                raw, re.DOTALL,
            )
            if not tweet_blocks:
                continue

            # Extract usernames
            usernames = re.findall(
                r'<a class="username"[^>]*>@([^<]+)</a>',
                raw,
            )

            formatted = []
            for i, block in enumerate(tweet_blocks[:10], 1):
                text = re.sub(r'<[^>]+>', ' ', block)
                text = html.unescape(text).strip()
                if len(text) > 400:
                    text = text[:400] + "..."
                user = f"@{usernames[i-1]}" if i <= len(usernames) else "@unknown"
                formatted.append(f"{i}. **{user}**: {text}")

            if formatted:
                return (
                    f"**Twitter/X search results for: {query}**\n"
                    f"(via Nitter — may be incomplete)\n\n"
                    + "\n\n".join(formatted)
                )
        except Exception:
            continue

    return None


# ============================================================================
# Python Exec Tool
# ============================================================================

def tool_python_exec(code: str) -> str:
    """Execute Python code in a sandboxed subprocess."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=PYTHON_TIMEOUT,
            cwd=tempfile.gettempdir(),
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]: {result.stderr}"
        if not output.strip():
            output = "(no output)"
        if len(output) > PYTHON_OUTPUT_MAX:
            output = output[:PYTHON_OUTPUT_MAX] + "\n[... output truncated ...]"
        return output
    except subprocess.TimeoutExpired:
        return f"Error: Code execution timed out after {PYTHON_TIMEOUT}s"
    except Exception as e:
        return f"Error executing code: {str(e)}"


# ============================================================================
# arXiv Search (free API, no auth)
# ============================================================================

async def tool_arxiv_search(query: str, max_results: int = 5) -> str:
    """Search arXiv for academic papers using the arXiv API."""
    try:
        max_results = min(max_results, 10)
        async with _get_semaphore("arxiv"):
            client = _get_http_client()
            resp = await client.get(
                "http://export.arxiv.org/api/query",
                params={
                    "search_query": f"all:{query}",
                    "start": 0,
                    "max_results": max_results,
                    "sortBy": "relevance",
                    "sortOrder": "descending",
                },
                timeout=20.0,
            )
        if resp.status_code != 200:
            return f"[TOOL_ERROR] arXiv search failed: HTTP {resp.status_code}. This is a technical failure, NOT 'no results found'."

        text = resp.text
        entries = re.findall(r'<entry>(.*?)</entry>', text, re.DOTALL)
        if not entries:
            return "No arXiv papers found."

        formatted = []
        for i, entry in enumerate(entries, 1):
            title_m = re.search(r'<title>(.*?)</title>', entry, re.DOTALL)
            title = title_m.group(1).strip().replace('\n', ' ') if title_m else "Unknown"
            summary_m = re.search(r'<summary>(.*?)</summary>', entry, re.DOTALL)
            summary = summary_m.group(1).strip()[:300] if summary_m else ""
            id_m = re.search(r'<id>(.*?)</id>', entry)
            arxiv_url = id_m.group(1).strip() if id_m else ""
            authors = re.findall(r'<name>(.*?)</name>', entry)
            author_str = ", ".join(authors[:3])
            if len(authors) > 3:
                author_str += f" et al. ({len(authors)} authors)"
            published_m = re.search(r'<published>(.*?)</published>', entry)
            published = published_m.group(1)[:10] if published_m else ""

            formatted.append(
                f"{i}. **{title}**\n"
                f"   Authors: {author_str}\n"
                f"   Published: {published}\n"
                f"   URL: {arxiv_url}\n"
                f"   Abstract: {summary}"
            )

        return "\n\n".join(formatted)

    except httpx.TimeoutException:
        return "[TOOL_ERROR] arXiv search timed out. This is a technical failure, NOT 'no results found'."
    except Exception as e:
        return f"[TOOL_ERROR] arXiv search failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# Wayback Machine Fetch
# ============================================================================

async def tool_wayback_fetch(url: str) -> str:
    """Fetch an archived version of a URL from the Wayback Machine."""
    try:
        client = _get_http_client()
        avail_resp = await client.get(
            "https://archive.org/wayback/available",
            params={"url": url},
            timeout=15.0,
        )
        if avail_resp.status_code != 200:
            return f"[TOOL_ERROR] Wayback Machine failed: HTTP {avail_resp.status_code}. This is a technical failure, NOT 'no results found'."

        avail_data = avail_resp.json()
        snapshots = avail_data.get("archived_snapshots", {})
        closest = snapshots.get("closest", {})
        if not closest or not closest.get("available"):
            return f"No archived version found for {url}"

        archive_url = closest.get("url", "")
        timestamp = closest.get("timestamp", "")

        content = await _fetch_webpage(archive_url)
        return (
            f"**Wayback Machine archive** (captured: {timestamp})\n"
            f"Original URL: {url}\n"
            f"Archive URL: {archive_url}\n\n{content}"
        )

    except httpx.TimeoutException:
        return "[TOOL_ERROR] Wayback Machine timed out. This is a technical failure, NOT 'no results found'."
    except Exception as e:
        return f"[TOOL_ERROR] Wayback Machine failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# Wikidata Query
# ============================================================================

async def tool_wikidata_query(entity: str) -> str:
    """Query Wikidata for structured facts about an entity."""
    try:
        sem = _get_semaphore("wikidata")
        async with sem:
            client = _get_http_client()
            search_resp = await client.get(
                "https://www.wikidata.org/w/api.php",
                params={
                    "action": "wbsearchentities",
                    "search": entity,
                    "language": "en",
                    "format": "json",
                    "limit": 3,
                },
                timeout=15.0,
                headers={"User-Agent": "EconomyDocumentary/1.0 (research tool)"},
            )
        if search_resp.status_code != 200:
            return f"[TOOL_ERROR] Wikidata search failed: HTTP {search_resp.status_code}. This is a technical failure, NOT 'no results found'."

        search_data = search_resp.json()
        results = search_data.get("search", [])
        if not results:
            return f"No Wikidata entity found for '{entity}'"

        formatted = []
        for r in results:
            qid = r.get("id", "")
            label = r.get("label", "")
            desc = r.get("description", "")
            url = f"https://www.wikidata.org/wiki/{qid}"
            formatted.append(
                f"- **{label}** ({qid}): {desc}\n  URL: {url}"
            )

        top_qid = results[0].get("id", "")
        if top_qid:
            async with sem:
                entity_resp = await client.get(
                    "https://www.wikidata.org/w/api.php",
                    params={
                        "action": "wbgetentities",
                        "ids": top_qid,
                        "languages": "en",
                        "format": "json",
                        "props": "labels|descriptions|claims",
                    },
                    timeout=15.0,
                    headers={"User-Agent": "EconomyDocumentary/1.0 (research tool)"},
                )
            if entity_resp.status_code == 200:
                entity_data = entity_resp.json()
                ent_info = entity_data.get("entities", {}).get(top_qid, {})
                claims = ent_info.get("claims", {})
                claim_strs = []
                for prop_id, claim_list in list(claims.items())[:10]:
                    for claim in claim_list[:1]:
                        mainsnak = claim.get("mainsnak", {})
                        datavalue = mainsnak.get("datavalue", {})
                        value = datavalue.get("value", "")
                        if isinstance(value, dict):
                            value = value.get("id", str(value))
                        claim_strs.append(f"  {prop_id}: {value}")
                if claim_strs:
                    formatted.append(f"\nTop claims for {top_qid}:\n" + "\n".join(claim_strs[:8]))

        return "\n".join(formatted)

    except httpx.TimeoutException:
        return "[TOOL_ERROR] Wikidata query timed out. This is a technical failure, NOT 'no results found'."
    except Exception as e:
        return f"[TOOL_ERROR] Wikidata query failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# Web Search (unified — delegates to search_providers)
# ============================================================================

async def tool_web_search(query: str) -> str:
    """Unified web search using multi_search from search_providers.

    Fan-out to DuckDuckGo, Brave, Tavily, Google concurrently.
    No moderation gate — the research proxy must be able to search any topic.
    """
    try:
        results = await multi_search(query, max_results=15)
        if not results:
            return "No results found."
        return _format_search_result_objects(results)
    except Exception as e:
        return f"[TOOL_ERROR] Web search failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# Hacker News Search (Algolia API — free, no auth)
# ============================================================================

async def tool_hackernews_search(query: str, sort_by: str = "relevance", time_range: str = "") -> str:
    """Search Hacker News via the Algolia API.

    Covers stories, comments, and Ask HN / Show HN posts.
    Free API, no authentication required.
    """
    try:
        async with _get_semaphore("hackernews"):
            client = _get_http_client()
            endpoint = "search" if sort_by == "relevance" else "search_by_date"
            params: dict[str, str | int] = {
                "query": query,
                "hitsPerPage": 15,
                "tags": "(story,comment)",
            }
            # Time range filtering via numericFilters
            if time_range:
                now = int(time.time())
                range_map = {
                    "day": 86400,
                    "week": 604800,
                    "month": 2592000,
                    "year": 31536000,
                }
                seconds = range_map.get(time_range, 0)
                if seconds:
                    params["numericFilters"] = f"created_at_i>{now - seconds}"

            resp = await client.get(
                f"https://hn.algolia.com/api/v1/{endpoint}",
                params=params,
                timeout=15.0,
            )
        if resp.status_code != 200:
            return f"[TOOL_ERROR] Hacker News search failed: HTTP {resp.status_code}. This is a technical failure, NOT 'no results found'."

        data = resp.json()
        hits = data.get("hits", [])
        if not hits:
            return f"No Hacker News results for: {query}"

        formatted = []
        for i, hit in enumerate(hits[:15], 1):
            title = hit.get("title") or hit.get("story_title") or ""
            comment_text = hit.get("comment_text") or ""
            author = hit.get("author", "unknown")
            points = hit.get("points") if hit.get("points") is not None else 0
            created = hit.get("created_at", "")[:10]
            obj_id = hit.get("objectID", "")
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}"
            hn_url = f"https://news.ycombinator.com/item?id={obj_id}"

            if comment_text:
                # Strip HTML from comments
                comment_text = re.sub(r'<[^>]+>', ' ', comment_text)
                comment_text = html.unescape(comment_text).strip()
                if len(comment_text) > 400:
                    comment_text = comment_text[:400] + "..."
                formatted.append(
                    f"{i}. **Comment by {author}** [{created}] (on: {title or 'thread'})\n"
                    f"   HN: {hn_url}\n"
                    f"   {comment_text}"
                )
            else:
                formatted.append(
                    f"{i}. **{title}** by {author} [{created}] ({points} points)\n"
                    f"   URL: {url}\n"
                    f"   HN: {hn_url}"
                )

        return "\n\n".join(formatted)

    except httpx.TimeoutException:
        return "[TOOL_ERROR] Hacker News search timed out. This is a technical failure, NOT 'no results found'."
    except Exception as e:
        return f"[TOOL_ERROR] Hacker News search failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# Stack Exchange Search (SE API v2.3 — free, no auth for read)
# ============================================================================

async def tool_stackexchange_search(query: str, site: str = "stackoverflow", sort: str = "relevance") -> str:
    """Search Stack Exchange sites for Q&A content.

    Covers hundreds of niche communities: stackoverflow, superuser, serverfault,
    askubuntu, math, physics, chemistry, biology, electronics, diy, cooking,
    gaming, rpg, worldbuilding, etc.

    Free API, no authentication required for read access.
    """
    try:
        async with _get_semaphore("stackexchange"):
            client = _get_http_client()
            resp = await client.get(
                "https://api.stackexchange.com/2.3/search/advanced",
                params={
                    "q": query,
                    "site": site,
                    "sort": sort,
                    "order": "desc",
                    "pagesize": 10,
                    "filter": "withbody",
                },
                timeout=15.0,
                headers={"Accept-Encoding": "gzip"},
            )
        if resp.status_code != 200:
            return f"[TOOL_ERROR] Stack Exchange search failed: HTTP {resp.status_code}. This is a technical failure (likely rate-limiting), NOT 'no results found'."

        data = resp.json()
        items = data.get("items", [])
        if not items:
            return f"No results on {site} for: {query}"

        formatted = []
        for i, item in enumerate(items[:10], 1):
            title = html.unescape(item.get("title", ""))
            score = item.get("score", 0)
            answers = item.get("answer_count", 0)
            is_answered = item.get("is_answered", False)
            link = item.get("link", "")
            tags = ", ".join(item.get("tags", [])[:5])
            creation = datetime.fromtimestamp(
                item.get("creation_date", 0), tz=timezone.utc
            ).strftime("%Y-%m-%d") if item.get("creation_date") else ""

            # Extract body text (HTML -> plain text)
            body = item.get("body", "")
            if body:
                body = re.sub(r'<[^>]+>', ' ', body)
                body = html.unescape(body).strip()
                if len(body) > 400:
                    body = body[:400] + "..."

            status = "ANSWERED" if is_answered else f"{answers} answers"
            formatted.append(
                f"{i}. **{title}** [score: {score}, {status}] [{creation}]\n"
                f"   Tags: {tags}\n"
                f"   URL: {link}\n"
                f"   {body}"
            )

        quota_remaining = data.get("quota_remaining", "?")
        return "\n\n".join(formatted) + f"\n\n[API quota remaining: {quota_remaining}]"

    except httpx.TimeoutException:
        return "[TOOL_ERROR] Stack Exchange search timed out. This is a technical failure, NOT 'no results found'."
    except Exception as e:
        return f"[TOOL_ERROR] Stack Exchange search failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# PubMed / Biomedical Search (NCBI E-utilities — free, no auth)
# ============================================================================

async def tool_pubmed_search(query: str, max_results: int = 10) -> str:
    """Search PubMed for biomedical and life science literature.

    Uses NCBI E-utilities (esearch + efetch). Covers medical journals,
    clinical trials, pharmacology, biochemistry, genetics, epidemiology,
    public health, and more. Free API, no authentication required.
    """
    max_results = min(max_results, 15)
    try:
        client = _get_http_client()

        # Step 1: Search for PMIDs
        async with _get_semaphore("pubmed"):
            search_resp = await client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params={
                    "db": "pubmed",
                    "term": query,
                    "retmax": max_results,
                    "retmode": "json",
                    "sort": "relevance",
                },
                timeout=15.0,
            )
        if search_resp.status_code != 200:
            return f"[TOOL_ERROR] PubMed search failed: HTTP {search_resp.status_code}. This is a technical failure, NOT 'no results found'."

        search_data = search_resp.json()
        id_list = search_data.get("esearchresult", {}).get("idlist", [])
        if not id_list:
            return f"No PubMed results for: {query}"

        # Step 2: Fetch article summaries
        async with _get_semaphore("pubmed"):
            fetch_resp = await client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                params={
                    "db": "pubmed",
                    "id": ",".join(id_list),
                    "retmode": "json",
                },
                timeout=15.0,
            )
        if fetch_resp.status_code != 200:
            return f"[TOOL_ERROR] PubMed fetch failed: HTTP {fetch_resp.status_code}. This is a technical failure, NOT 'no results found'."

        fetch_data = fetch_resp.json()
        results = fetch_data.get("result", {})

        formatted = []
        for i, pmid in enumerate(id_list, 1):
            article = results.get(pmid, {})
            if not article or isinstance(article, str):
                continue

            title = article.get("title", "No title")
            authors_list = article.get("authors", [])
            authors = ", ".join(
                a.get("name", "") for a in authors_list[:3]
            )
            if len(authors_list) > 3:
                authors += f" et al. ({len(authors_list)} authors)"

            journal = article.get("fulljournalname") or article.get("source", "")
            pub_date = article.get("pubdate", "")
            doi = ""
            for aid in article.get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = aid.get("value", "")
                    break

            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            doi_line = f"\n   DOI: https://doi.org/{doi}" if doi else ""

            formatted.append(
                f"{i}. **{title}**\n"
                f"   Authors: {authors}\n"
                f"   Journal: {journal} [{pub_date}]\n"
                f"   PMID: {pmid} | URL: {url}{doi_line}"
            )

        return "\n\n".join(formatted)

    except httpx.TimeoutException:
        return "[TOOL_ERROR] PubMed search timed out. This is a technical failure, NOT 'no results found'."
    except Exception as e:
        return f"[TOOL_ERROR] PubMed search failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# Wikipedia Full-Text Search (MediaWiki API — free, no auth)
# ============================================================================

async def tool_wikipedia_search(query: str, limit: int = 8) -> str:
    """Search Wikipedia for article content via the MediaWiki API.

    Returns article extracts with full text snippets, not just titles.
    Covers the entire English Wikipedia. Free API, no authentication required.
    """
    limit = min(limit, 15)
    try:
        async with _get_semaphore("wikipedia"):
            client = _get_http_client()
            resp = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": limit,
                    "srprop": "snippet|timestamp|wordcount",
                    "format": "json",
                },
                timeout=15.0,
                headers={"User-Agent": "EconomyDocumentary/1.0 (research tool)"},
            )
        if resp.status_code != 200:
            return f"[TOOL_ERROR] Wikipedia search failed: HTTP {resp.status_code}. This is a technical failure, NOT 'no results found'."

        data = resp.json()
        results = data.get("query", {}).get("search", [])
        if not results:
            return f"No Wikipedia results for: {query}"

        formatted = []
        for i, result in enumerate(results[:limit], 1):
            title = result.get("title", "")
            snippet = result.get("snippet", "")
            # Strip HTML from snippet
            snippet = re.sub(r'<[^>]+>', '', snippet)
            snippet = html.unescape(snippet).strip()
            wordcount = result.get("wordcount", 0)
            timestamp = result.get("timestamp", "")[:10]
            url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"

            formatted.append(
                f"{i}. **{title}** [{timestamp}] ({wordcount:,} words)\n"
                f"   URL: {url}\n"
                f"   {snippet}"
            )

        return "\n\n".join(formatted)

    except httpx.TimeoutException:
        return "[TOOL_ERROR] Wikipedia search timed out. This is a technical failure, NOT 'no results found'."
    except Exception as e:
        return f"[TOOL_ERROR] Wikipedia search failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# Archive.org Full-Text Search (Internet Archive — free, no auth)
# ============================================================================

async def tool_archiveorg_search(query: str, media_type: str = "", max_results: int = 10) -> str:
    """Search the Internet Archive's full-text index across all collections.

    Covers books, magazines, government documents, academic papers, audio,
    video, software, and web archives. This is NOT the Wayback Machine URL
    lookup — this searches the actual content of archived materials.

    Free API, no authentication required.
    """
    max_results = min(max_results, 15)
    try:
        async with _get_semaphore("archiveorg"):
            client = _get_http_client()
            params: dict[str, str | int] = {
                "q": query,
                "rows": max_results,
                "output": "json",
                "fl[]": "identifier,title,creator,date,description,mediatype,downloads",
            }
            if media_type:
                params["q"] = f"{query} AND mediatype:{media_type}"

            resp = await client.get(
                "https://archive.org/advancedsearch.php",
                params=params,
                timeout=20.0,
                headers={"User-Agent": "EconomyDocumentary/1.0 (research tool)"},
            )
        if resp.status_code != 200:
            return f"[TOOL_ERROR] Archive.org search failed: HTTP {resp.status_code}. This is a technical failure, NOT 'no results found'."

        data = resp.json()
        docs = data.get("response", {}).get("docs", [])
        if not docs:
            return f"No Archive.org results for: {query}"

        formatted = []
        for i, doc in enumerate(docs[:max_results], 1):
            title = doc.get("title", "No title")
            if isinstance(title, list):
                title = title[0] if title else "No title"
            creator = doc.get("creator", "Unknown")
            if isinstance(creator, list):
                creator = ", ".join(creator[:3])
            date = doc.get("date", "")[:10] if doc.get("date") else ""
            media = doc.get("mediatype", "")
            identifier = doc.get("identifier", "")
            downloads = doc.get("downloads", 0)
            description = doc.get("description", "")
            if isinstance(description, list):
                description = " ".join(description)
            if description:
                description = re.sub(r'<[^>]+>', ' ', description)
                description = html.unescape(description).strip()
                if len(description) > 300:
                    description = description[:300] + "..."

            url = f"https://archive.org/details/{identifier}"
            formatted.append(
                f"{i}. **{title}** by {creator} [{date}] ({media}, {downloads} downloads)\n"
                f"   URL: {url}\n"
                f"   {description}"
            )

        return "\n\n".join(formatted)

    except httpx.TimeoutException:
        return "[TOOL_ERROR] Archive.org search timed out. This is a technical failure, NOT 'no results found'."
    except Exception as e:
        return f"[TOOL_ERROR] Archive.org search failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# Niche Forum Search (via multi_search with site: operators)
# ============================================================================

_FORUM_SITE_TARGETS = [
    "site:forums.somethingawful.com",
    "site:forum.bodybuilding.com",
    "site:boards.straightdope.com",
    "site:discourse.org",
    "site:community.cloudflare.com",
    "site:forum.xda-developers.com",
    "site:forums.anandtech.com",
    "site:arstechnica.com/civis",
    "site:forums.hardwarezone.com.sg",
    "site:kiwifarms.net",
    "site:resetera.com",
    "site:neogaf.com",
    "site:overclock.net",
    "site:head-fi.org",
    "site:avsforum.com",
    "site:forum.lowyat.net",
]


async def tool_forum_search(query: str, forum_url: str = "") -> str:
    """Search niche internet forums via multi_search with site-targeting.

    If forum_url is provided, searches that specific forum.
    Otherwise, searches across a curated list of popular niche forums
    (SomethingAwful, Bodybuilding.com, XDA, Head-Fi, AVSForum, etc.)
    plus general forum-targeted queries.
    """
    try:
        results_all: list[dict] = []
        errors: list[str] = []

        if forum_url:
            # Search specific forum
            site_query = f"site:{forum_url.replace('https://', '').replace('http://', '').rstrip('/')} {query}"
            try:
                search_results = await multi_search(site_query, max_results=10)
                results_all.extend(
                    {"title": r.title, "url": r.url, "content": r.snippet} for r in search_results
                )
            except Exception as e:
                errors.append(f"site-specific search failed: {e}")
        else:
            # Search across curated forum list in batches
            batch_size = 5
            for batch_start in range(0, min(len(_FORUM_SITE_TARGETS), 15), batch_size):
                batch = _FORUM_SITE_TARGETS[batch_start:batch_start + batch_size]
                site_clause = " OR ".join(batch)
                forum_query = f"({site_clause}) {query}"
                try:
                    batch_results = await multi_search(forum_query, max_results=10)
                    results_all.extend(
                        {"title": r.title, "url": r.url, "content": r.snippet} for r in batch_results
                    )
                except Exception as e:
                    errors.append(f"batch {batch_start // batch_size + 1} failed: {e}")
                    continue

            # Also try a generic forum query
            try:
                generic_results = await multi_search(
                    f"{query} forum discussion thread", max_results=10
                )
                seen_urls = {r.get("url", "") for r in results_all}
                for r in generic_results:
                    if r.url not in seen_urls:
                        results_all.append({"title": r.title, "url": r.url, "content": r.snippet})
                        seen_urls.add(r.url)
            except Exception as e:
                errors.append(f"generic forum query failed: {e}")

        if not results_all:
            if errors:
                return (
                    f"[TOOL_ERROR] Forum search failed for: {query}. "
                    f"All {len(errors)} queries failed: {'; '.join(errors[:3])}. "
                    f"This is a technical failure, NOT 'no results found'."
                )
            return f"No forum results for: {query}"

        # Deduplicate by URL
        seen: set[str] = set()
        unique: list[dict] = []
        for r in results_all:
            url = r.get("url", "")
            if url and url not in seen:
                seen.add(url)
                unique.append(r)

        return _format_search_results(unique[:15], source_label="forum") or f"No forum results for: {query}"

    except Exception as e:
        return f"[TOOL_ERROR] Forum search failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# Google Scholar Search (via multi_search with academic site: operators)
# ============================================================================

async def tool_scholar_search(query: str) -> str:
    """Search Google Scholar for academic papers, citations, and theses.

    Uses multi_search with academic site targeting. Broader coverage
    than arXiv alone — includes journals, conference proceedings,
    theses, patents, and court opinions.
    """
    try:
        # Primary: academic site targeting
        academic_query = (
            f"({query}) (site:scholar.google.com OR site:semanticscholar.org "
            f"OR site:researchgate.net OR site:academia.edu OR site:ssrn.com "
            f"OR site:jstor.org OR site:ncbi.nlm.nih.gov)"
        )
        results = await multi_search(academic_query, max_results=15)
    except httpx.TimeoutException:
        return "[TOOL_ERROR] Scholar search timed out. This is a technical failure, NOT 'no results found'."
    except Exception as e:
        return f"[TOOL_ERROR] Scholar search failed: {str(e)}. This is a technical failure, NOT 'no results found'."

    if not results:
        # Fallback: broader academic search
        try:
            results = await multi_search(f"{query} academic paper research", max_results=10)
        except Exception:
            pass

    if not results:
        return f"No scholar results for: {query}"

    return _format_search_result_objects(results[:15], source_label="scholar")


# ============================================================================
# Substack Search (via multi_search with site:substack.com)
# ============================================================================

async def tool_substack_search(query: str) -> str:
    """Search Substack newsletters for long-form analysis and independent journalism.

    Uses multi_search with site:substack.com targeting. Covers investigative
    journalism, expert commentary, niche analysis, and independent reporting
    that doesn't appear in mainstream media.
    """
    try:
        # Primary: site-targeted search
        results = await multi_search(f"site:substack.com {query}", max_results=10)

        # Supplement with broader substack query
        try:
            extra = await multi_search(f"site:*.substack.com {query}", max_results=5)
            seen_urls = {r.url for r in results}
            for r in extra:
                if r.url not in seen_urls:
                    results.append(r)
                    seen_urls.add(r.url)
        except Exception:
            pass

        if not results:
            return f"No Substack results for: {query}"

        return _format_search_result_objects(results[:15], source_label="substack") or f"No Substack results for: {query}"

    except httpx.TimeoutException:
        return "[TOOL_ERROR] Substack search timed out. This is a technical failure, NOT 'no results found'."
    except Exception as e:
        return f"[TOOL_ERROR] Substack search failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# Telegram Search (via multi_search with site: operators)
# ============================================================================

async def tool_telegram_search(query: str, platform: str = "") -> str:
    """Search for Telegram channel/group content indexed on the public web.

    Uses multi_search to find Telegram content via t.me links, Telegram channel
    aggregators (tgstat.com, telemetr.io), and cached Telegram messages.
    This does NOT access Telegram's private API — only publicly indexed content.
    """
    try:
        results_all: list[SearchResult] = []
        errors: list[str] = []

        # Search t.me links directly
        try:
            r1 = await multi_search(f"site:t.me {query}", max_results=10)
            results_all.extend(r1)
        except Exception as e:
            errors.append(f"t.me search failed: {e}")

        # Search Telegram aggregator sites
        for site in ["tgstat.com", "telemetr.io", "telegramchannels.me"]:
            try:
                r = await multi_search(f"site:{site} {query}", max_results=5)
                seen = {x.url for x in results_all}
                for item in r:
                    if item.url not in seen:
                        results_all.append(item)
                        seen.add(item.url)
            except Exception as e:
                errors.append(f"{site} search failed: {e}")

        # Generic telegram query
        try:
            r3 = await multi_search(f"{query} telegram channel group", max_results=10)
            seen = {x.url for x in results_all}
            for item in r3:
                if item.url not in seen:
                    results_all.append(item)
                    seen.add(item.url)
        except Exception as e:
            errors.append(f"generic telegram query failed: {e}")

        if not results_all:
            if errors:
                return (
                    f"[TOOL_ERROR] Telegram search failed for: {query}. "
                    f"All {len(errors)} queries failed: {'; '.join(errors[:3])}. "
                    f"This is a technical failure, NOT 'no results found'."
                )
            return f"No Telegram results for: {query}"

        # Deduplicate by URL
        dedup_seen: set[str] = set()
        unique: list[SearchResult] = []
        for item in results_all:
            if item.url and item.url not in dedup_seen:
                dedup_seen.add(item.url)
                unique.append(item)

        return _format_search_result_objects(unique[:15], source_label="telegram") or f"No Telegram results for: {query}"

    except Exception as e:
        return f"[TOOL_ERROR] Telegram search failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# Darknet Market Search (OSINT via multi_search)
# ============================================================================

async def tool_darknet_market_search(query: str) -> str:
    """Search for darknet market intelligence via publicly indexed OSINT sources.

    Searches darknet market discussion forums, review sites, and OSINT
    aggregators that are indexed on the clearnet. Does NOT access .onion
    sites directly — only publicly available intelligence about darknet markets.
    """
    try:
        results_all: list[SearchResult] = []
        errors: list[str] = []

        # Search known clearnet darknet-market discussion/review sites
        osint_sites = [
            "dread.support",
            "darknetlive.com",
            "darknetmarkets.org",
            "dark.fail",
        ]
        for site in osint_sites:
            try:
                r = await multi_search(f"site:{site} {query}", max_results=5)
                results_all.extend(r)
            except Exception as e:
                errors.append(f"{site} failed: {e}")

        # Generic darknet market OSINT queries
        osint_queries = [
            f"{query} darknet market review",
            f"{query} dark web vendor",
            f"{query} onion market",
        ]
        seen = {x.url for x in results_all}
        for oq in osint_queries:
            try:
                r = await multi_search(oq, max_results=5)
                for item in r:
                    if item.url not in seen:
                        results_all.append(item)
                        seen.add(item.url)
            except Exception as e:
                errors.append(f"query '{oq[:40]}' failed: {e}")

        if not results_all:
            if errors:
                return (
                    f"[TOOL_ERROR] Darknet market search failed for: {query}. "
                    f"All {len(errors)} queries failed: {'; '.join(errors[:3])}. "
                    f"This is a technical failure, NOT 'no results found'."
                )
            return f"No darknet market OSINT results for: {query}"

        # Deduplicate
        dedup_seen: set[str] = set()
        unique: list[SearchResult] = []
        for r in results_all:
            if r.url and r.url not in dedup_seen:
                dedup_seen.add(r.url)
                unique.append(r)

        return (
            _format_search_result_objects(unique[:15], source_label="darknet_osint")
            or f"No darknet market OSINT results for: {query}"
        )

    except Exception as e:
        return f"[TOOL_ERROR] Darknet market search failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# Site-Filtered Search Tools (via multi_search with site: filters)
# ============================================================================


async def tool_facebook_search(
    query: str, result_type: str = "posts", platform: str = ""
) -> str:
    """Search Facebook pages, groups, and posts via multi_search site filters.

    Public Facebook content indexed by search engines. Private groups
    and personal profiles are not accessible.
    """
    try:
        # Build type-specific URL filter
        type_filter = ""
        if result_type == "groups":
            type_filter = "inurl:groups"
        elif result_type == "pages":
            type_filter = "inurl:pages"

        site_query = (
            f"({query}) (site:facebook.com OR site:fb.com) {type_filter}"
        ).strip()
        results = await multi_search(site_query, max_results=15)
        if not results:
            results = await multi_search(
                f"facebook {result_type} {query}", max_results=10
            )
        return (
            _format_search_result_objects(results[:15], source_label="facebook")
            or f"No Facebook results for: {query}"
        )
    except httpx.TimeoutException:
        return "Facebook search error: request timed out"
    except Exception as e:
        return f"Facebook search error: {str(e)}"


async def tool_discord_search(query: str) -> str:
    """Search public Discord server content via multi_search site filters.

    Targets Discord message archives and server listing sites.
    Private server content is not accessible.
    """
    try:
        site_query = (
            f"({query}) (site:discord.com OR site:discordapp.com "
            f"OR site:top.gg OR site:disboard.org)"
        )
        results = await multi_search(site_query, max_results=15)
        if not results:
            results = await multi_search(f"discord {query}", max_results=10)
        return (
            _format_search_result_objects(results[:15], source_label="discord")
            or f"No Discord results for: {query}"
        )
    except httpx.TimeoutException:
        return "Discord search error: request timed out"
    except Exception as e:
        return f"Discord search error: {str(e)}"


async def tool_signal_search(query: str) -> str:
    """Search Signal-related public content via multi_search.

    Signal is end-to-end encrypted; this only finds public references
    to Signal groups, channels, and discussions on indexable websites.
    """
    try:
        site_query = (
            f'({query}) (site:signal.group OR site:signal.org '
            f'OR "signal group" OR "signal channel")'
        )
        results = await multi_search(site_query, max_results=15)
        if not results:
            results = await multi_search(f"signal app {query}", max_results=10)
        return (
            _format_search_result_objects(results[:15], source_label="signal")
            or f"No Signal results for: {query}"
        )
    except httpx.TimeoutException:
        return "Signal search error: request timed out"
    except Exception as e:
        return f"Signal search error: {str(e)}"


async def tool_whatsapp_search(query: str) -> str:
    """Search WhatsApp group invites and public references via multi_search.

    WhatsApp is end-to-end encrypted; this only finds publicly-shared
    group invite links and references to WhatsApp communities.
    """
    try:
        site_query = (
            f'({query}) (site:chat.whatsapp.com OR "whatsapp group" '
            f'OR "join whatsapp")'
        )
        results = await multi_search(site_query, max_results=15)
        if not results:
            results = await multi_search(
                f"whatsapp group {query}", max_results=10
            )
        return (
            _format_search_result_objects(results[:15], source_label="whatsapp")
            or f"No WhatsApp results for: {query}"
        )
    except httpx.TimeoutException:
        return "WhatsApp search error: request timed out"
    except Exception as e:
        return f"WhatsApp search error: {str(e)}"


async def tool_crunchbase_search(query: str) -> str:
    """Search Crunchbase for company/startup information via multi_search.

    Finds company profiles, funding rounds, and organizational data
    indexed from Crunchbase.
    """
    try:
        results = await multi_search(f"site:crunchbase.com {query}", max_results=15)
        if not results:
            results = await multi_search(
                f"crunchbase {query}", max_results=10
            )
        return (
            _format_search_result_objects(results[:15], source_label="crunchbase")
            or f"No Crunchbase results for: {query}"
        )
    except httpx.TimeoutException:
        return "Crunchbase search error: request timed out"
    except Exception as e:
        return f"Crunchbase search error: {str(e)}"


async def tool_trustpilot_search(query: str) -> str:
    """Search Trustpilot for company reviews and ratings via multi_search.

    Finds business reviews, customer feedback, and trust scores
    indexed from Trustpilot.
    """
    try:
        results = await multi_search(f"site:trustpilot.com {query}", max_results=15)
        if not results:
            results = await multi_search(
                f"trustpilot review {query}", max_results=10
            )
        return (
            _format_search_result_objects(results[:15], source_label="trustpilot")
            or f"No Trustpilot results for: {query}"
        )
    except httpx.TimeoutException:
        return "Trustpilot search error: request timed out"
    except Exception as e:
        return f"Trustpilot search error: {str(e)}"


# ============================================================================
# WHOIS Lookup (RDAP protocol)
# ============================================================================

async def tool_whois_lookup(domain: str = "", query: str = "") -> str:
    """Look up WHOIS information for a domain via public WHOIS APIs."""
    target = domain or query
    if not target:
        return "WHOIS lookup error: no domain provided"
    # Strip protocol and path
    target = re.sub(r'^https?://', '', target).split('/')[0].strip()
    try:
        client = _get_http_client()
        resp = await client.get(
            f"https://rdap.org/domain/{target}",
            timeout=15.0,
            headers={"Accept": "application/rdap+json"},
        )
        if resp.status_code != 200:
            # Fallback to multi_search
            results = await multi_search(f"whois {target}", max_results=5)
            return (
                _format_search_result_objects(results[:5], source_label="whois")
                or f"WHOIS lookup failed for {target}: HTTP {resp.status_code}"
            )

        data = resp.json()
        parts = [f"**WHOIS for {target}:**"]

        name = data.get("ldhName", target)
        parts.append(f"Domain: {name}")

        status = data.get("status", [])
        if status:
            parts.append(f"Status: {', '.join(status[:5])}")

        events = data.get("events", [])
        for ev in events:
            action = ev.get("eventAction", "")
            date = ev.get("eventDate", "")
            if action and date:
                parts.append(f"{action}: {date[:10]}")

        nameservers = data.get("nameservers", [])
        if nameservers:
            ns_list = [ns.get("ldhName", "") for ns in nameservers[:4]]
            parts.append(f"Nameservers: {', '.join(ns_list)}")

        entities = data.get("entities", [])
        for ent in entities[:3]:
            roles = ent.get("roles", [])
            vcard = ent.get("vcardArray", [None, []])
            if len(vcard) > 1:
                for item in vcard[1]:
                    if len(item) >= 4 and item[0] == "fn":
                        parts.append(f"{', '.join(roles)}: {item[3]}")
                        break

        return "\n".join(parts)

    except httpx.TimeoutException:
        return f"WHOIS lookup error: request timed out for {target}"
    except Exception as e:
        return f"WHOIS lookup error: {str(e)}"


# ============================================================================
# YouTube Search (via multi_search with site: targeting)
# ============================================================================

async def tool_youtube_search(query: str) -> str:
    """Search YouTube for video content — tutorials, discussions, practitioner knowledge.

    YouTube is a severely underutilized source of deep knowledge: practitioner
    teardowns, community discussions, conference talks, investigative videos,
    and how-to content that rarely appears in text sources. Returns video titles,
    URLs, and descriptions. Use youtube_transcript to get full spoken content.
    """
    try:
        results = await multi_search(f"site:youtube.com {query}", max_results=15)

        if not results:
            return f"No YouTube results for: {query}"

        return _format_search_result_objects(results[:15], source_label="youtube") or f"No YouTube results for: {query}"

    except httpx.TimeoutException:
        return "[TOOL_ERROR] YouTube search timed out. This is a technical failure, NOT 'no results found'."
    except Exception as e:
        return f"[TOOL_ERROR] YouTube search failed: {str(e)}. This is a technical failure, NOT 'no results found'."


# ============================================================================
# YouTube Deep Extraction Pipeline
# ============================================================================
# Three layers of video content extraction:
# 1. youtube_transcript — full spoken content via youtube-transcript-api
# 2. youtube_video_metadata — title, description, chapters, comments via yt-dlp
# 3. youtube_video_analyze — visual analysis (stubbed, Qwen Omni not available)


def _extract_video_id(url_or_id: str) -> Optional[str]:
    """Extract YouTube video ID from a URL or return it if already an ID."""
    url_or_id = url_or_id.strip()
    # Already a bare video ID (11 chars, alphanumeric + - _)
    if re.match(r'^[A-Za-z0-9_-]{11}$', url_or_id):
        return url_or_id
    # Standard youtube.com/watch?v=ID
    m = re.search(r'[?&]v=([A-Za-z0-9_-]{11})', url_or_id)
    if m:
        return m.group(1)
    # Short youtu.be/ID
    m = re.search(r'youtu\.be/([A-Za-z0-9_-]{11})', url_or_id)
    if m:
        return m.group(1)
    # Embed youtube.com/embed/ID
    m = re.search(r'youtube\.com/embed/([A-Za-z0-9_-]{11})', url_or_id)
    if m:
        return m.group(1)
    # Shorts youtube.com/shorts/ID
    m = re.search(r'youtube\.com/shorts/([A-Za-z0-9_-]{11})', url_or_id)
    if m:
        return m.group(1)
    return None


async def _whisperx_transcribe(video_id: str, lang: str = "en") -> str:
    """Transcribe YouTube audio using WhisperX for high-accuracy timestamps.

    Downloads audio via yt-dlp, runs WhisperX for word-level aligned
    transcription. Returns formatted transcript or empty string on failure.
    """
    loop = asyncio.get_running_loop()
    yt_url = f"https://www.youtube.com/watch?v={video_id}"

    def _run_whisperx() -> str:
        try:
            import whisperx
        except ImportError:
            log.debug("WhisperX not installed, skipping")
            return ""

        try:
            import yt_dlp
        except ImportError:
            log.debug("yt-dlp not installed, cannot download audio for WhisperX")
            return ""

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "audio.wav")
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "format": "bestaudio/best",
                "outtmpl": os.path.join(tmpdir, "audio.%(ext)s"),
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "wav",
                    "preferredquality": "16",
                }],
                "max_filesize": 200 * 1024 * 1024,  # 200MB cap
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([yt_url])
            except Exception as e:
                log.warning(f"WhisperX: yt-dlp download failed for {video_id}: {e}")
                return ""

            if not os.path.exists(audio_path):
                # yt-dlp may name it differently
                import glob as _glob
                wavs = _glob.glob(os.path.join(tmpdir, "*.wav"))
                if wavs:
                    audio_path = wavs[0]
                else:
                    log.debug("WhisperX: no WAV file produced by yt-dlp")
                    return ""

            # Detect device
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            compute_type = "float16" if device == "cuda" else "int8"

            # Load and transcribe
            try:
                model = whisperx.load_model(
                    "large-v3", device, compute_type=compute_type,
                    language=lang,
                )
                audio = whisperx.load_audio(audio_path)
                result = model.transcribe(audio, batch_size=16)
            except Exception as e:
                log.warning(f"WhisperX transcription failed for {video_id}: {e}")
                return ""

            # Alignment for word-level timestamps
            try:
                model_a, metadata = whisperx.load_align_model(
                    language_code=lang, device=device,
                )
                result = whisperx.align(
                    result["segments"], model_a, metadata, audio, device,
                    return_char_alignments=False,
                )
            except Exception as e:
                log.debug(f"WhisperX alignment failed (using unaligned): {e}")

            # Format output
            segments = result.get("segments", [])
            if not segments:
                return ""

            lines = []
            for seg in segments:
                start = seg.get("start", 0)
                text = seg.get("text", "").strip()
                if not text:
                    continue
                mins, secs = divmod(int(start), 60)
                hours, mins = divmod(mins, 60)
                if hours > 0:
                    ts = f"[{hours}:{mins:02d}:{secs:02d}]"
                else:
                    ts = f"[{mins}:{secs:02d}]"
                lines.append(f"{ts} {text}")

            full_text = "\n".join(lines)
            max_chars = 30000
            if len(full_text) > max_chars:
                full_text = (
                    full_text[:max_chars]
                    + f"\n\n[WHISPERX TRANSCRIPT TRUNCATED — {len(segments)} segments total]"
                )
            return f"YOUTUBE TRANSCRIPT (WhisperX) for {video_id}:\n{full_text}"

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _run_whisperx),
            timeout=300.0,  # WhisperX can take several minutes
        )
    except asyncio.TimeoutError:
        log.warning(f"WhisperX timed out for {video_id}")
        return ""
    except Exception as e:
        log.warning(f"WhisperX error for {video_id}: {e}")
        return ""


async def tool_youtube_transcript(url: str, lang: str = "en") -> str:
    """Extract the full transcript/subtitles from a YouTube video.

    Transcription priority:
      1. **WhisperX** — most accurate, word-level timestamps, handles any language.
         Requires: whisperx, torch, yt-dlp, ffmpeg.
      2. **LangChain YoutubeLoader** — uses YouTube's own captions (fast, no GPU).
      3. **youtube-transcript-api** — raw caption fallback with timestamps.

    YouTube videos are a MANDATORY data source for research. This tool extracts
    the actual spoken content — practitioner explanations, lecture content,
    interview dialogue, tutorial steps.
    """
    video_id = _extract_video_id(url)
    if not video_id:
        return f"Could not extract video ID from: {url}"

    # --- Priority 1: WhisperX (best accuracy) ---
    whisperx_result = await _whisperx_transcribe(video_id, lang)
    if whisperx_result:
        return whisperx_result

    loop = asyncio.get_running_loop()
    yt_url = f"https://www.youtube.com/watch?v={video_id}"

    def _fetch_transcript() -> str:
        # --- Priority 2: LangChain YoutubeLoader ---
        try:
            from langchain_community.document_loaders import YoutubeLoader

            loader = YoutubeLoader.from_youtube_url(
                yt_url,
                add_video_info=True,
                language=[lang, "en"],
                continue_on_failure=True,
            )
            docs = loader.load()
            if docs:
                meta = docs[0].metadata
                header_parts = []
                if meta.get("title"):
                    header_parts.append(f"TITLE: {meta['title']}")
                if meta.get("author"):
                    header_parts.append(f"CHANNEL: {meta['author']}")
                if meta.get("publish_date"):
                    header_parts.append(f"DATE: {meta['publish_date']}")
                if meta.get("length"):
                    m, s = divmod(int(meta["length"]), 60)
                    header_parts.append(f"DURATION: {m}:{s:02d}")
                if meta.get("view_count"):
                    header_parts.append(f"VIEWS: {meta['view_count']:,}")

                header = "\n".join(header_parts)
                content = docs[0].page_content

                # Cap output
                max_chars = 30000
                if len(content) > max_chars:
                    content = content[:max_chars] + "\n\n[TRANSCRIPT TRUNCATED]"

                return f"YOUTUBE TRANSCRIPT for {video_id}:\n{header}\n\n{content}"
        except Exception as e:
            log.debug(f"YoutubeLoader failed for {video_id}, falling back: {e}")

        # --- Priority 3: raw youtube-transcript-api with timestamps ---
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            return "youtube-transcript-api not installed"

        ytt = YouTubeTranscriptApi()

        try:
            snippets = ytt.fetch(video_id, languages=[lang, "en"])
        except Exception:
            try:
                transcript_list = ytt.list(video_id)
                available = list(transcript_list)
                if not available:
                    return f"No transcripts available for video {video_id}"
                snippets = available[0].fetch()
            except Exception as e:
                return f"Transcript fetch failed for {video_id}: {e}"

        lines = []
        for s in snippets:
            start = s.start if hasattr(s, 'start') else s.get("start", 0)
            text = s.text if hasattr(s, 'text') else s.get("text", "")
            mins, secs = divmod(int(start), 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                ts = f"[{hours}:{mins:02d}:{secs:02d}]"
            else:
                ts = f"[{mins}:{secs:02d}]"
            lines.append(f"{ts} {text}")

        full_text = "\n".join(lines)
        max_chars = 30000
        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + f"\n\n[TRANSCRIPT TRUNCATED — {len(lines)} segments total]"

        return f"YOUTUBE TRANSCRIPT for {video_id}:\n{full_text}"

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_transcript),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        return f"Transcript extraction timed out for {video_id}"
    except Exception as e:
        return f"Transcript extraction error for {video_id}: {e}"


# ============================================================================
# YouTube Video Metadata (yt-dlp)
# ============================================================================

async def tool_youtube_video_metadata(url: str) -> str:
    """Extract rich metadata from a YouTube video using yt-dlp.

    Returns: title, channel, upload date, view count, like count,
    full description, chapter markers, tags, categories, duration,
    and top comments (if available).
    """
    video_id = _extract_video_id(url)
    if not video_id:
        return f"Could not extract video ID from: {url}"

    loop = asyncio.get_running_loop()

    def _fetch_metadata() -> str:
        try:
            import yt_dlp
        except ImportError:
            return "yt-dlp not installed"

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": False,
            "getcomments": True,
            "extractor_args": {"youtube": {"max_comments": ["30", "0", "0", "0"]}},
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}",
                    download=False,
                )
        except Exception as e:
            return f"yt-dlp extraction failed for {video_id}: {e}"

        if not info:
            return f"No metadata returned for {video_id}"

        parts = []
        parts.append(f"TITLE: {info.get('title', 'Unknown')}")
        parts.append(f"CHANNEL: {info.get('channel', info.get('uploader', 'Unknown'))}")
        parts.append(f"UPLOAD DATE: {info.get('upload_date', 'Unknown')}")

        duration = info.get("duration")
        if duration:
            m, s = divmod(int(duration), 60)
            h, m = divmod(m, 60)
            parts.append(f"DURATION: {h}:{m:02d}:{s:02d}" if h else f"DURATION: {m}:{s:02d}")

        view_count = info.get("view_count")
        if view_count is not None:
            parts.append(f"VIEWS: {view_count:,}")

        like_count = info.get("like_count")
        if like_count is not None:
            parts.append(f"LIKES: {like_count:,}")

        tags = info.get("tags") or []
        if tags:
            parts.append(f"TAGS: {', '.join(tags[:20])}")

        categories = info.get("categories") or []
        if categories:
            parts.append(f"CATEGORIES: {', '.join(categories)}")

        description = info.get("description", "")
        if description:
            # Cap description at 3000 chars
            desc_text = description[:3000]
            if len(description) > 3000:
                desc_text += "... [truncated]"
            parts.append(f"\nDESCRIPTION:\n{desc_text}")

        # Chapter markers — gold for navigating long videos
        chapters = info.get("chapters") or []
        if chapters:
            parts.append("\nCHAPTERS:")
            for ch in chapters:
                start = ch.get("start_time", 0)
                m, s = divmod(int(start), 60)
                h, m = divmod(m, 60)
                ts = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
                parts.append(f"  {ts} - {ch.get('title', 'Untitled')}")

        # Comments — community knowledge, corrections, additional context
        comments = info.get("comments") or []
        if comments:
            parts.append(f"\nTOP COMMENTS ({len(comments)}):")
            for c in comments[:20]:
                author = c.get("author", "Anonymous")
                text = (c.get("text") or "")[:500]
                likes = c.get("like_count", 0)
                prefix = f"  [{likes} likes]" if likes else "  "
                parts.append(f"{prefix} @{author}: {text}")

        return "\n".join(parts)

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_metadata),
            timeout=60.0,
        )
    except asyncio.TimeoutError:
        return f"Metadata extraction timed out for {video_id}"
    except Exception as e:
        return f"Metadata extraction error for {video_id}: {e}"


# ============================================================================
# YouTube Video Analyze (STUBBED — Qwen Omni not available)
# ============================================================================

async def tool_youtube_video_analyze(
    url: str,
    question: str = "",
) -> str:
    """Analyze a YouTube video's visual content using a vision-capable model.

    NOTE: This tool is not available in this environment. The Qwen Omni
    vision model endpoint is not configured. Use youtube_transcript for
    spoken content and youtube_video_metadata for description/comments.
    """
    video_id = _extract_video_id(url)
    if not video_id:
        return f"Could not extract video ID from: {url}"

    return (
        "Video visual analysis not available: Qwen Omni vision model endpoint "
        "is not configured in this environment. "
        "Use youtube_transcript for spoken content and "
        "youtube_video_metadata for description, chapters, and comments instead."
    )
