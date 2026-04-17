"""
Before-tool callback -- per-provider rate limiting + dashboard tracking.

Ported from MiroThinker. Adapted for documentary pipeline tools
(OTIO, TTS, video generation, Vast.ai, assembly).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

from google.adk.tools import ToolContext

from dashboard import get_active_collector

logger = logging.getLogger(__name__)

# -- Per-provider rate limiting -------------------------------------------------
_GPU_CONCURRENCY = int(os.environ.get("GPU_CONCURRENCY", "1"))
_TTS_CONCURRENCY = int(os.environ.get("TTS_CONCURRENCY", "2"))
_VASTAI_CONCURRENCY = int(os.environ.get("VASTAI_CONCURRENCY", "3"))

_provider_semaphores: Dict[str, threading.Semaphore] = {
    "gpu": threading.Semaphore(_GPU_CONCURRENCY),
    "tts": threading.Semaphore(_TTS_CONCURRENCY),
    "vastai": threading.Semaphore(_VASTAI_CONCURRENCY),
}

# Map tool names to provider keys
_TOOL_TO_PROVIDER: Dict[str, str] = {
    "generate_video_clip": "gpu",
    "probe_clip": "gpu",
    "generate_narration": "tts",
    "align_narration": "tts",
    "provision_gpu_vm": "vastai",
    "check_vm_status": "vastai",
    "terminate_vm": "vastai",
    "list_active_vms": "vastai",
}


def _get_provider(tool_name: str) -> Optional[str]:
    """Return provider key for a tool, or None if not rate-limited."""
    return _TOOL_TO_PROVIDER.get(tool_name)


def before_tool_callback(
    tool: Any, args: Dict[str, Any], tool_context: ToolContext
) -> Optional[Dict[str, Any]]:
    """ADK before_tool_callback.

    Returns None to allow the tool call to proceed.
    """
    tool_name: str = tool.name if hasattr(tool, "name") else str(tool)
    call_id = getattr(tool_context, "function_call_id", "") or tool_name

    # -- Per-provider rate limiting --------------------------------------------
    provider = _get_provider(tool_name)
    if provider:
        sem = _provider_semaphores[provider]
        # Block until semaphore is available (with 120s timeout to avoid deadlocks)
        acquired = sem.acquire(blocking=True, timeout=120)
        if acquired:
            tool_context.state[f"_provider_sem_{call_id}"] = provider
            logger.debug("Provider semaphore acquired: %s", provider)
        else:
            logger.warning(
                "Provider semaphore timeout for %s after 120s — rejecting tool call",
                provider,
            )
            return {"error": f"Rate limit timeout: {provider} concurrency limit reached. Retry later."}

    # -- Dashboard: track tool start -------------------------------------------
    tool_context.state[f"_tool_start_time_{call_id}"] = time.time()
    _c = get_active_collector()
    if _c:
        agent_name = getattr(tool_context, "agent_name", "pipeline")
        args_summary = str(args)[:300] if args else ""
        _c.tool_start(tool_name, agent_name, args_summary)

    return None
