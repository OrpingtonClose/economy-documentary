"""Rate-limit plugin -- GPU / TTS / VastAI semaphores.

Replaces server/callbacks/before_tool.py and server/callbacks/after_tool.py.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from strands.hooks.events import AfterToolCallEvent, BeforeToolCallEvent
from strands.plugins import Plugin, hook

logger = logging.getLogger(__name__)

# Tool-name → semaphore-group mapping
_TOOL_GROUPS: dict[str, str] = {
    "generate_video_clip": "gpu",
    "probe_clip": "gpu",
    "generate_narration": "tts",
    "align_narration": "tts",
    "provision_gpu_vm": "vastai",
    "check_vm_status": "vastai",
    "terminate_vm": "vastai",
    "list_active_vms": "vastai",
}

_DEFAULT_LIMITS: dict[str, int] = {
    "gpu": int(os.environ.get("GPU_CONCURRENCY", "1")),
    "tts": int(os.environ.get("TTS_CONCURRENCY", "2")),
    "vastai": int(os.environ.get("VASTAI_CONCURRENCY", "3")),
}


class RateLimitPlugin(Plugin):
    """Acquires/releases per-group semaphores around tool calls."""

    name = "rate_limit"

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        effective = {**_DEFAULT_LIMITS, **(limits or {})}
        self._semaphores: dict[str, threading.Semaphore] = {
            group: threading.Semaphore(count) for group, count in effective.items()
        }
        self._active: dict[int, tuple[str, float]] = {}
        self._lock = threading.Lock()
        super().__init__()

    def _group_for(self, tool_name: str) -> str | None:
        return _TOOL_GROUPS.get(tool_name)

    @hook
    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        """Acquire semaphore for the tool's resource group."""
        tool_name = event.tool_use.get("name", "")
        group = self._group_for(tool_name)
        if not group:
            return

        sem = self._semaphores.get(group)
        if sem:
            sem.acquire()
            tid = threading.get_ident()
            with self._lock:
                self._active[tid] = (group, time.monotonic())
            logger.debug(
                "tool=<%s>, group=<%s> | acquired rate-limit semaphore",
                tool_name,
                group,
            )

    @hook
    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        """Release semaphore for the tool's resource group."""
        tool_name = event.tool_use.get("name", "")
        group = self._group_for(tool_name)
        if not group:
            return

        sem = self._semaphores.get(group)
        if sem:
            tid = threading.get_ident()
            start = 0.0
            with self._lock:
                entry = self._active.pop(tid, None)
                if entry:
                    start = entry[1]
            sem.release()
            elapsed = time.monotonic() - start if start else 0.0
            logger.debug(
                "tool=<%s>, group=<%s>, elapsed_ms=<%d> | released rate-limit semaphore",
                tool_name,
                group,
                int(elapsed * 1000),
            )
