"""
Central configuration — all settings hardcoded or read from files.

No environment variables. Configuration is passed as explicit parameters,
read from files, or hardcoded.
"""

from __future__ import annotations

import os
from pathlib import Path


# ── Helper ───────────────────────────────────────────────────
_KEY_DIR = Path("/Users/orpington/api_keys/LLMS")


def _read_key(filename: str) -> str:
    """Read an API key from a file in the key directory."""
    path = _KEY_DIR / filename
    if path.exists():
        return path.read_text().strip()
    return ""


# ── Topic Configuration ──────────────────────────────────────
DOCUMENTARY_TOPIC = ""

# ── Paths ────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DOCUMENTARY_LOG_DIR", "/tmp/documentary-pipeline"))
CORPUS_DIR = DATA_DIR / "corpus"
TRANSCRIPT_DIR = CORPUS_DIR / "transcripts"
ENRICHMENT_DIR = DATA_DIR / "enrichment"
TIMELINE_DIR = DATA_DIR / "timelines"
TTS_OUTPUT_DIR = DATA_DIR / "tts"
VIDEO_OUTPUT_DIR = DATA_DIR / "video"
FINDINGS_DIR = DATA_DIR / "findings"
DASHBOARD_DB_DIR = DATA_DIR / "dashboard"

# ── API Keys (read from files only) ──────────────────────────
# Allowed LLM: deepseek-v4-flash ONLY
DEEPSEEK_API = _read_key("deepseek_api.txt")

# Infrastructure
VAST_API_KEY = ""

# Proxy Services
BRIGHTDATA_KEY = ""
BRIGHTDATA_CUSTOMER_ID = "hl_dc044bf4"
BRIGHTDATA_ZONE = "mcp_unlocker"
OXYLABS_USERNAME = ""
OXYLABS_PASSWORD = ""

# ── Concurrency ──────────────────────────────────────────────
MAX_CONCURRENT_LLM = 4
MAX_CONTEXT_TOKENS = 120000
HARD_CONTEXT_LIMIT = 180000
TOOL_RESULT_MAX_CHARS = 30000
GPU_CONCURRENCY = 2
TTS_CONCURRENCY = 3
VASTAI_CONCURRENCY = 2
TREE_MAX_CONCURRENT = 4
MAX_SUBAGENT_TURNS = 12

# ── Feature Flags ────────────────────────────────────────────
ADK_DEBUG = False
PHOENIX_ENABLED = False
PHOENIX_COLLECTOR_ENDPOINT = ""

# ── Plugin Configuration ─────────────────────────────────────
CONTEXT_INVOCATIONS_TO_KEEP = 10
TOOL_MAX_RETRIES = 2
