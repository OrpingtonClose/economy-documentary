"""
Tool execution: retry wrappers, PDF text extraction, execute_tool dispatcher,
knowledge graph stubs, and parallel tool execution.

Ported from deep-search-portal/proxies/tools/tool_executor.py.

Key differences from the original:
  - Imports tools from local modules (.tools, .web_fetch, .search_tools2, .search_providers)
  - Simple dict-based cache instead of SQLite search_cache module
  - asyncio.Semaphore for rate governance instead of rate_governor module
  - In-memory health tracker (dict) instead of SQLite tool_health
  - Knowledge graph tools stubbed (no knowledge_client dependency)
  - Social media tools (social_media_search, reddit_search, instagram_search,
    tiktok_search, linkedin_search, youtube_search) are mapped to search_tools2
    implementations instead of social_media_scrapers
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Optional

import httpx
from .llm import _get_client

log = logging.getLogger("enrichment")


# ============================================================================
# Lazy imports — tool functions are imported on first use to avoid circular
# imports and to allow modules that don't exist yet to be stubbed gracefully.
# ============================================================================

def _get_tool_imports():
    """Lazy-import all tool functions.  Returns a dict of name -> callable."""
    # These are populated once and cached on the module
    global _TOOL_FUNCS
    if _TOOL_FUNCS is not None:
        return _TOOL_FUNCS

    funcs: dict = {}

    # --- Local tools module (perplexity, fred, web_search, etc.) ---
    try:
        from .tools import (
            perplexity_verify,
            fred_lookup,
            web_search as tool_web_search_local,
            economic_search,
            news_search as tool_news_search_local,
            wolfram_compute,
            web_read,
        )
        funcs["perplexity_verify"] = perplexity_verify
        funcs["fred_lookup"] = fred_lookup
        funcs["web_search_local"] = tool_web_search_local
        funcs["economic_search"] = economic_search
        funcs["news_search_local"] = tool_news_search_local
        funcs["wolfram_compute"] = wolfram_compute
        funcs["web_read"] = web_read
    except ImportError as e:
        log.debug(f"Could not import .tools: {e}")

    # --- Web fetch ---
    try:
        from .web_fetch import (
            enhanced_web_fetch,
            tool_4plebs_search,
            tool_b4k_search,
            tool_warosu_search,
        )
        funcs["enhanced_web_fetch"] = enhanced_web_fetch
        funcs["tool_4plebs_search"] = tool_4plebs_search
        funcs["tool_b4k_search"] = tool_b4k_search
        funcs["tool_warosu_search"] = tool_warosu_search
    except ImportError as e:
        log.debug(f"Could not import .web_fetch: {e}")

    # --- search_tools2 (the big collection) ---
    try:
        from .search_tools2 import (
            tool_twitter_search,
            tool_python_exec,
            tool_arxiv_search,
            tool_wayback_fetch,
            tool_wikidata_query,
            tool_web_search,
            tool_hackernews_search,
            tool_stackexchange_search,
            tool_pubmed_search,
            tool_wikipedia_search,
            tool_archiveorg_search,
            tool_forum_search,
            tool_scholar_search,
            tool_substack_search,
            tool_telegram_search,
            tool_darknet_market_search,
            tool_facebook_search,
            tool_discord_search,
            tool_signal_search,
            tool_whatsapp_search,
            tool_crunchbase_search,
            tool_trustpilot_search,
            tool_whois_lookup,
            tool_youtube_search,
            tool_youtube_transcript,
            tool_youtube_video_metadata,
            tool_youtube_video_analyze,
        )
        funcs["tool_twitter_search"] = tool_twitter_search
        funcs["tool_python_exec"] = tool_python_exec
        funcs["tool_arxiv_search"] = tool_arxiv_search
        funcs["tool_wayback_fetch"] = tool_wayback_fetch
        funcs["tool_wikidata_query"] = tool_wikidata_query
        funcs["tool_web_search"] = tool_web_search
        funcs["tool_hackernews_search"] = tool_hackernews_search
        funcs["tool_stackexchange_search"] = tool_stackexchange_search
        funcs["tool_pubmed_search"] = tool_pubmed_search
        funcs["tool_wikipedia_search"] = tool_wikipedia_search
        funcs["tool_archiveorg_search"] = tool_archiveorg_search
        funcs["tool_forum_search"] = tool_forum_search
        funcs["tool_scholar_search"] = tool_scholar_search
        funcs["tool_substack_search"] = tool_substack_search
        funcs["tool_telegram_search"] = tool_telegram_search
        funcs["tool_darknet_market_search"] = tool_darknet_market_search
        funcs["tool_facebook_search"] = tool_facebook_search
        funcs["tool_discord_search"] = tool_discord_search
        funcs["tool_signal_search"] = tool_signal_search
        funcs["tool_whatsapp_search"] = tool_whatsapp_search
        funcs["tool_crunchbase_search"] = tool_crunchbase_search
        funcs["tool_trustpilot_search"] = tool_trustpilot_search
        funcs["tool_whois_lookup"] = tool_whois_lookup
        funcs["tool_youtube_search"] = tool_youtube_search
        funcs["tool_youtube_transcript"] = tool_youtube_transcript
        funcs["tool_youtube_video_metadata"] = tool_youtube_video_metadata
        funcs["tool_youtube_video_analyze"] = tool_youtube_video_analyze
    except ImportError as e:
        log.debug(f"Could not import .search_tools2: {e}")

    # --- Search providers (for news_search fallback) ---
    try:
        from .search_providers import multi_search_news, format_results
        funcs["multi_search_news"] = multi_search_news
        funcs["format_results"] = format_results
    except ImportError as e:
        log.debug(f"Could not import .search_providers: {e}")

    _TOOL_FUNCS = funcs
    return funcs

_TOOL_FUNCS: dict | None = None


# ============================================================================
# Simple In-Memory Cache  (replaces search_cache SQLite module)
# ============================================================================

_result_cache: dict[tuple[str, str], str] = {}


def _cache_get(tool_name: str, query_key: str) -> Optional[str]:
    """Check the in-memory cache for a previous result."""
    return _result_cache.get((tool_name, query_key))


def _cache_put(tool_name: str, query_key: str, result: str) -> None:
    """Store a result in the in-memory cache."""
    _result_cache[(tool_name, query_key)] = result


def _normalize_query(val: str) -> str:
    """Normalize a query string for cache key purposes.

    Case-fold, strip punctuation, sort words so near-duplicate queries
    hit the cache.
    """
    val = val.lower().strip()
    words = re.split(r'\W+', val)
    words = [w for w in words if w]
    words.sort()
    return " ".join(words)


# ============================================================================
# Rate Governor  (replaces rate_governor module — simple asyncio.Semaphore)
# ============================================================================

_global_semaphore = asyncio.Semaphore(10)


class _governed_request:
    """Async context manager for rate-governed tool execution."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name

    async def __aenter__(self):
        await _global_semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        _global_semaphore.release()
        return False


# ============================================================================
# Tool Health Tracker  (replaces tool_health SQLite module)
# ============================================================================

_tool_health: dict[str, dict] = {}


def _record_outcome(tool_name: str, success: bool, error: str = "") -> None:
    """Record a tool execution outcome in the in-memory health tracker."""
    if tool_name not in _tool_health:
        _tool_health[tool_name] = {
            "successes": 0,
            "failures": 0,
            "consecutive_failures": 0,
        }
    entry = _tool_health[tool_name]
    if success:
        entry["successes"] += 1
        entry["consecutive_failures"] = 0
    else:
        entry["failures"] += 1
        entry["consecutive_failures"] += 1
        if error:
            entry["last_error"] = error[:500]


def get_tool_health(tool_name: str = "") -> dict:
    """Get health stats for a tool or all tools."""
    if tool_name:
        return _tool_health.get(tool_name, {
            "successes": 0, "failures": 0, "consecutive_failures": 0,
        })
    return dict(_tool_health)


# ============================================================================
# Retry Wrapper for Tool Execution
# ============================================================================

def _simplify_query(query: str) -> str:
    """Strip a long query down to its core keywords for retry.

    Community/underground search APIs often choke on long natural-language
    queries.  This extracts the 3-5 most significant words.
    """
    stop = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "dare", "ought",
        "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
        "as", "into", "through", "during", "before", "after", "above",
        "below", "between", "out", "off", "over", "under", "again",
        "further", "then", "once", "here", "there", "when", "where", "why",
        "how", "all", "both", "each", "few", "more", "most", "other", "some",
        "such", "no", "nor", "not", "only", "own", "same", "so", "than",
        "too", "very", "just", "about", "what", "which", "who", "whom",
        "this", "that", "these", "those", "i", "me", "my", "we", "our",
        "you", "your", "he", "him", "his", "she", "her", "it", "its",
        "they", "them", "their", "and", "but", "or", "if",
    }
    words = [w for w in re.split(r'\W+', query.lower()) if w and w not in stop]
    return " ".join(words[:5]) if words else query


# Tools that benefit from retry with simplified queries
_RARE_SOURCE_TOOLS = {
    "chan_4plebs_search", "chan_b4k_search", "chan_warosu_search",
    "forum_search", "telegram_search", "darknet_market_search",
    "twitter_search", "substack_search", "youtube_search",
    "youtube_transcript", "youtube_video_metadata", "youtube_video_analyze",
}


async def _retry_tool_call(
    coro_factory,
    max_retries: int = 2,
    backoff_base: float = 1.0,
) -> str:
    """Retry a tool call with exponential backoff on transient failures.

    coro_factory is a zero-arg callable that returns a new coroutine each time.
    Only retries on timeout and server errors, not on valid empty results.
    """
    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            result = await coro_factory()
            # Don't retry on valid "no results" — only on actual errors
            prefix = result.lower()[:80]
            if "error" not in prefix and "failed" not in prefix and "timed out" not in prefix:
                return result
            if attempt < max_retries:
                last_error = result
                await asyncio.sleep(backoff_base * (2 ** attempt))
                continue
            return result
        except httpx.TimeoutException:
            last_error = "request timed out"
            if attempt < max_retries:
                await asyncio.sleep(backoff_base * (2 ** attempt))
                continue
        except Exception as e:
            last_error = str(e)
            if attempt < max_retries:
                await asyncio.sleep(backoff_base * (2 ** attempt))
                continue

    return f"Tool failed after {max_retries + 1} attempts: {last_error}"


async def _retry_rare_tool(
    tool_name: str,
    arguments: dict,
    req_id: str = "",
) -> str:
    """Retry a rare/community source tool with query simplification.

    For community/underground tools, if the first attempt returns no useful
    results, retry once with a simplified (keyword-only) query.  This handles
    the common case where long natural-language queries return empty on APIs
    that expect short keyword searches.
    """
    result = await execute_tool(tool_name, arguments, req_id)
    prefix = result.lower()[:120]

    # If result looks empty or error-ish, retry with simplified query
    is_empty = (
        "no results" in prefix
        or "0 results" in prefix
        or len(result.strip()) < 30
    )
    is_error = "error" in prefix or "failed" in prefix or "timed out" in prefix

    if (is_empty or is_error) and "query" in arguments:
        original_q = arguments["query"]
        simplified = _simplify_query(original_q)
        if simplified != original_q.lower().strip():
            log.info(
                f"[{req_id}] Retrying {tool_name} with simplified query: "
                f"'{original_q[:60]}' → '{simplified}'"
            )
            retry_args = {**arguments, "query": simplified}
            result2 = await execute_tool(tool_name, retry_args, req_id)
            if len(result2.strip()) > len(result.strip()):
                return result2

    return result


# ============================================================================
# PDF Text Extraction
# ============================================================================

async def _extract_pdf_text(url: str) -> Optional[str]:
    """Download and extract text from a PDF document.

    Uses PyMuPDF (fitz) if available, falls back to pdfplumber.
    Returns extracted text or None on failure.
    """
    try:
        client = _get_client()
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/2.0)"},
        )
        if resp.status_code != 200:
            return None

        content_type = resp.headers.get("content-type", "")
        if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
            return None

        pdf_bytes = resp.content

        # Try PyMuPDF first (fastest)
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            pages_text = []
            for page_num in range(min(doc.page_count, 50)):  # Cap at 50 pages
                page = doc[page_num]
                pages_text.append(page.get_text())
            doc.close()
            text = "\n\n".join(pages_text).strip()
            if text:
                return text
        except ImportError:
            pass
        except Exception as e:
            log.debug(f"PyMuPDF extraction failed for {url}: {e}")

        # Fallback: pdfplumber
        try:
            import pdfplumber
            import io
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages_text = []
                for page in pdf.pages[:50]:
                    page_text = page.extract_text()
                    if page_text:
                        pages_text.append(page_text)
                text = "\n\n".join(pages_text).strip()
                if text:
                    return text
        except ImportError:
            pass
        except Exception as e:
            log.debug(f"pdfplumber extraction failed for {url}: {e}")

        return None
    except Exception as e:
        log.debug(f"PDF download failed for {url}: {e}")
        return None


# ============================================================================
# Knowledge Graph Stubs
# ============================================================================

async def tool_knowledge_graph_search(arguments: dict) -> str:
    """Search the knowledge graph (stubbed — not configured)."""
    return "Knowledge graph not configured in this environment."


async def tool_knowledge_discover(arguments: dict) -> str:
    """Run graph discovery algorithms (stubbed — not configured)."""
    return "Knowledge graph not configured in this environment."


# ============================================================================
# Inner Tool Dispatcher — routes tool_name to the correct function
# ============================================================================

async def _execute_tool_inner(tool_name: str, arguments: dict) -> str:
    """Route and execute a tool call (inner implementation)."""
    funcs = _get_tool_imports()

    if tool_name == "searxng_search":
        fn = funcs.get("tool_web_search")
        if fn:
            return await fn(arguments.get("query", ""))
        return "searxng_search: search_tools2 not available"

    elif tool_name == "news_search":
        # Try search_tools2's news search, fall back to local tools
        fn = funcs.get("news_search_local")
        if fn:
            return await fn(arguments.get("query", ""))
        # Fallback to search_providers
        multi_news = funcs.get("multi_search_news")
        fmt = funcs.get("format_results")
        if multi_news and fmt:
            results = await multi_news(arguments.get("query", ""))
            return fmt(results) if results else f"No news results for: {arguments.get('query', '')}"
        return "news_search: no search backend available"

    elif tool_name == "fetch_webpage":
        fn = funcs.get("enhanced_web_fetch")
        if fn:
            return await fn(
                arguments.get("url", ""),
                arguments.get("extract_info", ""),
            )
        # Fallback to web_read
        fn = funcs.get("web_read")
        if fn:
            return await fn(arguments.get("url", ""))
        return "fetch_webpage: no web fetch backend available"

    elif tool_name == "python_exec":
        fn = funcs.get("tool_python_exec")
        if fn:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, fn, arguments.get("code", ""))
        return "python_exec: not available"

    elif tool_name == "arxiv_search":
        fn = funcs.get("tool_arxiv_search")
        if fn:
            return await fn(
                arguments.get("query", ""),
                arguments.get("max_results", 5),
            )
        return "arxiv_search: not available"

    elif tool_name == "wayback_fetch":
        fn = funcs.get("tool_wayback_fetch")
        if fn:
            return await fn(arguments.get("url", ""))
        return "wayback_fetch: not available"

    elif tool_name == "wikidata_query":
        fn = funcs.get("tool_wikidata_query")
        if fn:
            return await fn(arguments.get("entity", ""))
        return "wikidata_query: not available"

    elif tool_name == "knowledge_graph_search":
        return await tool_knowledge_graph_search(arguments)

    elif tool_name == "knowledge_discover":
        return await tool_knowledge_discover(arguments)

    elif tool_name == "chan_4plebs_search":
        fn = funcs.get("tool_4plebs_search")
        if fn:
            return await fn(
                arguments.get("query", ""),
                arguments.get("board", "pol"),
            )
        return "chan_4plebs_search: not available"

    elif tool_name == "chan_b4k_search":
        fn = funcs.get("tool_b4k_search")
        if fn:
            return await fn(arguments.get("query", ""))
        return "chan_b4k_search: not available"

    elif tool_name == "chan_warosu_search":
        fn = funcs.get("tool_warosu_search")
        if fn:
            return await fn(
                arguments.get("query", ""),
                arguments.get("board", "g"),
            )
        return "chan_warosu_search: not available"

    elif tool_name == "twitter_search":
        fn = funcs.get("tool_twitter_search")
        if fn:
            return await fn(arguments.get("query", ""))
        return "twitter_search: not available"

    # --- Social media tools: mapped to search_tools2 implementations ---
    elif tool_name == "social_media_search":
        # Route by platform to search_tools2 functions
        platform = arguments.get("platform", "")
        query = arguments.get("query", "")
        if platform == "twitter":
            fn = funcs.get("tool_twitter_search")
            if fn:
                return await fn(query)
        elif platform == "reddit":
            # Use web search with reddit site filter
            fn = funcs.get("tool_web_search")
            if fn:
                return await fn(f"site:reddit.com {query}")
        elif platform == "instagram":
            fn = funcs.get("tool_web_search")
            if fn:
                return await fn(f"site:instagram.com {query}")
        elif platform == "tiktok":
            fn = funcs.get("tool_web_search")
            if fn:
                return await fn(f"site:tiktok.com {query}")
        elif platform == "linkedin":
            fn = funcs.get("tool_web_search")
            if fn:
                return await fn(f"site:linkedin.com {query}")
        elif platform == "youtube":
            fn = funcs.get("tool_youtube_search")
            if fn:
                return await fn(query)
        return f"social_media_search ({platform}): not available"

    elif tool_name == "reddit_search":
        fn = funcs.get("tool_web_search")
        if fn:
            query = arguments.get("query", "")
            subreddit = arguments.get("subreddit", "")
            if subreddit:
                return await fn(f"site:reddit.com/r/{subreddit} {query}")
            return await fn(f"site:reddit.com {query}")
        return "reddit_search: not available"

    elif tool_name == "instagram_search":
        fn = funcs.get("tool_web_search")
        if fn:
            return await fn(f"site:instagram.com {arguments.get('query', '')}")
        return "instagram_search: not available"

    elif tool_name == "tiktok_search":
        fn = funcs.get("tool_web_search")
        if fn:
            return await fn(f"site:tiktok.com {arguments.get('query', '')}")
        return "tiktok_search: not available"

    elif tool_name == "linkedin_search":
        fn = funcs.get("tool_web_search")
        if fn:
            return await fn(f"site:linkedin.com {arguments.get('query', '')}")
        return "linkedin_search: not available"

    elif tool_name == "youtube_search":
        fn = funcs.get("tool_youtube_search")
        if fn:
            return await fn(arguments.get("query", ""))
        # Fallback to web search
        fn = funcs.get("tool_web_search")
        if fn:
            return await fn(f"site:youtube.com {arguments.get('query', '')}")
        return "youtube_search: not available"

    elif tool_name == "hackernews_search":
        fn = funcs.get("tool_hackernews_search")
        if fn:
            return await fn(
                arguments.get("query", ""),
                arguments.get("sort_by", "relevance"),
                arguments.get("time_range", ""),
            )
        return "hackernews_search: not available"

    elif tool_name == "stackexchange_search":
        fn = funcs.get("tool_stackexchange_search")
        if fn:
            return await fn(
                arguments.get("query", ""),
                arguments.get("site", "stackoverflow"),
                arguments.get("sort", "relevance"),
            )
        return "stackexchange_search: not available"

    elif tool_name == "pubmed_search":
        fn = funcs.get("tool_pubmed_search")
        if fn:
            return await fn(
                arguments.get("query", ""),
                arguments.get("max_results", 10),
            )
        return "pubmed_search: not available"

    elif tool_name == "wikipedia_search":
        fn = funcs.get("tool_wikipedia_search")
        if fn:
            return await fn(
                arguments.get("query", ""),
                arguments.get("limit", 8),
            )
        return "wikipedia_search: not available"

    elif tool_name == "archiveorg_search":
        fn = funcs.get("tool_archiveorg_search")
        if fn:
            return await fn(
                arguments.get("query", ""),
                arguments.get("media_type", ""),
                arguments.get("max_results", 10),
            )
        return "archiveorg_search: not available"

    elif tool_name == "forum_search":
        fn = funcs.get("tool_forum_search")
        if fn:
            return await fn(
                arguments.get("query", ""),
                arguments.get("forum_url", ""),
            )
        return "forum_search: not available"

    elif tool_name == "scholar_search":
        fn = funcs.get("tool_scholar_search")
        if fn:
            return await fn(arguments.get("query", ""))
        return "scholar_search: not available"

    elif tool_name == "substack_search":
        fn = funcs.get("tool_substack_search")
        if fn:
            return await fn(arguments.get("query", ""))
        return "substack_search: not available"

    elif tool_name == "telegram_search":
        fn = funcs.get("tool_telegram_search")
        if fn:
            return await fn(
                arguments.get("query", ""),
                arguments.get("platform", ""),
            )
        return "telegram_search: not available"

    elif tool_name == "darknet_market_search":
        fn = funcs.get("tool_darknet_market_search")
        if fn:
            return await fn(arguments.get("query", ""))
        return "darknet_market_search: not available"

    elif tool_name == "facebook_search":
        fn = funcs.get("tool_facebook_search")
        if fn:
            return await fn(
                arguments.get("query", ""),
                arguments.get("result_type", "posts"),
                arguments.get("platform", ""),
            )
        return "facebook_search: not available"

    elif tool_name == "discord_search":
        fn = funcs.get("tool_discord_search")
        if fn:
            return await fn(arguments.get("query", ""))
        return "discord_search: not available"

    elif tool_name == "signal_search":
        fn = funcs.get("tool_signal_search")
        if fn:
            return await fn(arguments.get("query", ""))
        return "signal_search: not available"

    elif tool_name == "whatsapp_search":
        fn = funcs.get("tool_whatsapp_search")
        if fn:
            return await fn(arguments.get("query", ""))
        return "whatsapp_search: not available"

    elif tool_name == "crunchbase_search":
        fn = funcs.get("tool_crunchbase_search")
        if fn:
            return await fn(arguments.get("query", ""))
        return "crunchbase_search: not available"

    elif tool_name == "trustpilot_search":
        fn = funcs.get("tool_trustpilot_search")
        if fn:
            return await fn(arguments.get("query", ""))
        return "trustpilot_search: not available"

    elif tool_name == "whois_lookup":
        fn = funcs.get("tool_whois_lookup")
        if fn:
            return await fn(
                domain=arguments.get("domain", ""),
                query=arguments.get("query", ""),
            )
        return "whois_lookup: not available"

    elif tool_name == "youtube_transcript":
        fn = funcs.get("tool_youtube_transcript")
        if fn:
            return await fn(
                arguments.get("url", ""),
                arguments.get("lang", "en"),
            )
        return "youtube_transcript: not available"

    elif tool_name == "youtube_video_metadata":
        fn = funcs.get("tool_youtube_video_metadata")
        if fn:
            return await fn(arguments.get("url", ""))
        return "youtube_video_metadata: not available"

    elif tool_name == "youtube_video_analyze":
        fn = funcs.get("tool_youtube_video_analyze")
        if fn:
            return await fn(
                arguments.get("url", ""),
                arguments.get("question", ""),
            )
        return "youtube_video_analyze: not available"

    # --- Local economy-documentary tools (from .tools) ---
    elif tool_name == "perplexity_verify":
        fn = funcs.get("perplexity_verify")
        if fn:
            return await fn(arguments.get("claim", ""))
        return "perplexity_verify: not available"

    elif tool_name == "fred_lookup":
        fn = funcs.get("fred_lookup")
        if fn:
            return await fn(
                series_id=arguments.get("series_id", ""),
                search_text=arguments.get("search_text", ""),
            )
        return "fred_lookup: not available"

    elif tool_name == "web_search":
        fn = funcs.get("web_search_local")
        if fn:
            return await fn(
                arguments.get("query", ""),
                arguments.get("category", "general"),
            )
        return "web_search: not available"

    elif tool_name == "economic_search":
        fn = funcs.get("economic_search")
        if fn:
            return await fn(arguments.get("query", ""))
        return "economic_search: not available"

    elif tool_name == "wolfram_compute":
        fn = funcs.get("wolfram_compute")
        if fn:
            return await fn(arguments.get("query", ""))
        return "wolfram_compute: not available"

    elif tool_name == "web_read":
        fn = funcs.get("web_read")
        if fn:
            return await fn(arguments.get("url", ""))
        return "web_read: not available"

    else:
        return f"[TOOL_ERROR] Unknown tool: {tool_name}. This tool does not exist in the system."


# ============================================================================
# Cacheable / Governed Tool Sets
# ============================================================================

# Tools whose results are cacheable (search-type tools)
_CACHEABLE_TOOLS = {
    "searxng_search", "web_search", "news_search", "arxiv_search",
    "wikidata_query", "hackernews_search", "stackexchange_search",
    "pubmed_search", "wikipedia_search", "archiveorg_search",
    "forum_search", "scholar_search", "substack_search",
    "youtube_search", "youtube_transcript", "youtube_video_metadata",
    "twitter_search", "telegram_search", "darknet_market_search",
    "facebook_search", "discord_search", "signal_search",
    "whatsapp_search", "crunchbase_search", "trustpilot_search",
    "whois_lookup", "wayback_fetch",
    "social_media_search", "reddit_search", "instagram_search",
    "tiktok_search", "linkedin_search",
    "chan_4plebs_search", "chan_b4k_search", "chan_warosu_search",
}

# Tools that involve long-running local computation (e.g. WhisperX GPU
# transcription) and should NOT hold a global concurrency slot.
_UNGOVERNED_HEAVY_TOOLS: set[str] = {
    "youtube_transcript",      # WhisperX can take up to 300s of local GPU work
    "youtube_video_analyze",   # downloads + analyses video locally
}

# Tools that access the internet (governed by rate limiter).
# Excludes heavy local-compute tools that would starve the global semaphore.
_GOVERNED_TOOLS = (_CACHEABLE_TOOLS | {"fetch_webpage"}) - _UNGOVERNED_HEAVY_TOOLS


# ============================================================================
# Cache Key Extraction
# ============================================================================

def _extract_query_for_cache(tool_name: str, arguments: dict) -> str:
    """Extract a normalized cache-relevant string from tool arguments.

    Normalizes the primary query field (case-folding, stop-word removal,
    word-order sorting) so that near-duplicate queries hit the cache,
    while preserving all other parameters (subreddit, platform, etc.)
    to avoid collisions.
    """
    # Keys that contain the primary natural-language query
    _QUERY_KEYS = ("query", "search_query", "question", "term", "keywords")

    normalized = dict(arguments)
    for key in _QUERY_KEYS:
        val = normalized.get(key)
        if isinstance(val, str) and val and not val.startswith("http"):
            normalized[key] = _normalize_query(val)
    return json.dumps(normalized, sort_keys=True)


# ============================================================================
# Main execute_tool — with caching, rate governance, health tracking
# ============================================================================

async def execute_tool(
    tool_name: str,
    arguments: dict,
    req_id: str = "",
) -> str:
    """Route and execute a tool call with rate limiting, caching, and health tracking.

    Wraps the inner tool execution with:
      1. Search result cache (check before, store after)
      2. Rate governor (global concurrency semaphore)
      3. Tool health monitor (track success/failure rates)
    """
    # --- Step 1: Check cache for search tools ---
    if tool_name in _CACHEABLE_TOOLS:
        query_str = _extract_query_for_cache(tool_name, arguments)
        cached = _cache_get(tool_name, query_str)
        if cached is not None:
            log.debug(f"[{req_id}] Cache hit for {tool_name}: {query_str[:60]}")
            return cached

    # --- Step 2: Execute with rate governor ---
    try:
        if tool_name in _GOVERNED_TOOLS:
            async with _governed_request(tool_name):
                result = await _execute_tool_inner(tool_name, arguments)
        else:
            result = await _execute_tool_inner(tool_name, arguments)
    except Exception as e:
        error_str = str(e)
        _record_outcome(tool_name, success=False, error=error_str)
        return f"Tool error ({tool_name}): {error_str}"

    # --- Step 3: Record health outcome ---
    result_prefix = result[:80]
    is_error = (
        result_prefix.lower().startswith("error")
        or result_prefix.lower().startswith("failed")
        or "search error:" in result_prefix.lower()
        or "timed out" in result_prefix.lower()
        or result.startswith("[TOOL_ERROR]")
        or result.startswith("Tool error")
        or result.startswith("Unknown tool:")
    )
    if is_error:
        _record_outcome(tool_name, success=False, error=result[:500])
    else:
        _record_outcome(tool_name, success=True)

    # --- Step 4: Store in cache ---
    if tool_name in _CACHEABLE_TOOLS and not is_error:
        query_str = _extract_query_for_cache(tool_name, arguments)
        _cache_put(tool_name, query_str, result)

    return result


# ============================================================================
# Parallel Tool Execution
# ============================================================================

async def execute_tools_parallel(
    tool_calls_with_ids: list[tuple[str, str, dict]],
    req_id: str = "",
) -> list[tuple[str, str, str, float]]:
    """Execute multiple tool calls concurrently.

    Input:  [(call_id, tool_name, arguments), ...]
    Output: [(call_id, tool_name, result_string, duration_seconds), ...]
    """

    async def _run_one(tc_id: str, name: str, args: dict):
        t0 = time.monotonic()
        result = await execute_tool(name, args, req_id=req_id)
        return tc_id, name, result, time.monotonic() - t0

    tasks = [_run_one(tc_id, name, args) for tc_id, name, args in tool_calls_with_ids]
    return list(await asyncio.gather(*tasks))
