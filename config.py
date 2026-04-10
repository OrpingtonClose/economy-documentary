"""
Central configuration — all settings from environment variables.

This is the single source of truth for runtime configuration.
Import from here rather than reading env vars directly.
"""

from __future__ import annotations

import os
from pathlib import Path


# ── Topic Configuration ──────────────────────────────────────
# The documentary topic — set dynamically per run
DOCUMENTARY_TOPIC = os.environ.get("DOCUMENTARY_TOPIC", "")

# ── Paths ────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/documentary-pipeline"))
CORPUS_DIR = DATA_DIR / "corpus"
TRANSCRIPT_DIR = CORPUS_DIR / "transcripts"
ENRICHMENT_DIR = DATA_DIR / "enrichment"
TIMELINE_DIR = Path(os.environ.get("TIMELINE_DIR", str(DATA_DIR / "timelines")))
TTS_OUTPUT_DIR = Path(os.environ.get("TTS_OUTPUT_DIR", str(DATA_DIR / "tts")))
VIDEO_OUTPUT_DIR = Path(os.environ.get("VIDEO_OUTPUT_DIR", str(DATA_DIR / "video")))
FINDINGS_DIR = Path(os.environ.get("FINDINGS_DIR", str(DATA_DIR / "findings")))
DASHBOARD_DB_DIR = Path(os.environ.get("DASHBOARD_DB_DIR", str(DATA_DIR / "dashboard")))

# ── API Keys (env vars only — no file-based keys) ───────────
# LLM Providers
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
PERPLEXITY_API_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Search Providers
EXA_API_KEY = os.environ.get("EXA_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
BRAVE_SEARCH_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
GOOGLE_SEARCH_API_KEY = os.environ.get("GOOGLE_SEARCH_API_KEY", "")
GOOGLE_SEARCH_ENGINE_ID = os.environ.get("GOOGLE_SEARCH_ENGINE_ID", "")

# Data APIs
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
WOLFRAM_ALPHA_API_KEY = os.environ.get("WOLFRAM_ALPHA_API_KEY", "")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")

# GPU / Infrastructure
VAST_API_KEY = os.environ.get("VAST_API_KEY", "")

# Proxy Services
BRIGHTDATA_KEY = os.environ.get("BRIGHTDATA_KEY", "")
BRIGHTDATA_CUSTOMER_ID = os.environ.get("BRIGHTDATA_CUSTOMER_ID", "hl_dc044bf4")
BRIGHTDATA_ZONE = os.environ.get("BRIGHTDATA_ZONE", "mcp_unlocker")
OXYLABS_USERNAME = os.environ.get("OXYLABS_USERNAME", "")
OXYLABS_PASSWORD = os.environ.get("OXYLABS_PASSWORD", "")

# Observability
AGENTOPS_API_KEY = os.environ.get("AGENTOPS_API_KEY", "")

# ── ADK Model Configuration ─────────────────────────────────
ADK_MODEL = os.environ.get("ADK_MODEL", "gemini-2.5-flash")
ADK_SYNTHESIS_MODEL = os.environ.get("ADK_SYNTHESIS_MODEL", "")
ADK_THINKER_MODEL = os.environ.get("ADK_THINKER_MODEL", "")
ADK_VISION_MODEL = os.environ.get("ADK_VISION_MODEL", "")
OPENAI_API_BASE = os.environ.get("OPENAI_API_BASE", "")

# ── Concurrency ──────────────────────────────────────────────
MAX_CONCURRENT_LLM = int(os.environ.get("MAX_CONCURRENT_LLM", "4"))
MAX_CONTEXT_TOKENS = int(os.environ.get("MAX_CONTEXT_TOKENS", "120000"))
HARD_CONTEXT_LIMIT = int(os.environ.get("HARD_CONTEXT_LIMIT", "180000"))
TOOL_RESULT_MAX_CHARS = int(os.environ.get("TOOL_RESULT_MAX_CHARS", "30000"))
GPU_CONCURRENCY = int(os.environ.get("GPU_CONCURRENCY", "2"))
TTS_CONCURRENCY = int(os.environ.get("TTS_CONCURRENCY", "3"))
VASTAI_CONCURRENCY = int(os.environ.get("VASTAI_CONCURRENCY", "2"))
TREE_MAX_CONCURRENT = int(os.environ.get("TREE_MAX_CONCURRENT", "4"))
MAX_SUBAGENT_TURNS = int(os.environ.get("MAX_SUBAGENT_TURNS", "12"))

# ── Feature Flags ────────────────────────────────────────────
DOCUMENTARY_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "false").lower() == "true"
ADK_DEBUG = os.environ.get("ADK_DEBUG", "false").lower() == "true"
PHOENIX_ENABLED = os.environ.get("PHOENIX_ENABLED", "false").lower() == "true"
PHOENIX_COLLECTOR_ENDPOINT = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "")

# ── Plugin Configuration ─────────────────────────────────────
CONTEXT_INVOCATIONS_TO_KEEP = int(os.environ.get("CONTEXT_INVOCATIONS_TO_KEEP", "10"))
TOOL_MAX_RETRIES = int(os.environ.get("TOOL_MAX_RETRIES", "2"))


def _read_key(filename: str) -> str:
    """Read an API key from environment variable.

    This is a compatibility shim for existing code that used file-based keys.
    The filename is converted to an env var name:
      'minmax_api.txt' -> MINMAX_API
      'deepseek_api.txt' -> DEEPSEEK_API

    Args:
        filename: Original key filename (e.g., 'deepseek_api.txt').

    Returns:
        The API key string, or empty string if not set.
    """
    env_name = filename.replace(".txt", "").upper()
    return os.environ.get(env_name, "")
