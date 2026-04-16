"""Rate-limit plugin -- GPU / TTS / VastAI semaphores.

Replaces server/callbacks/before_tool.py and server/callbacks/after_tool.py.

Uses threading.local() for per-call state tracking (not thread IDs) because
before/after hooks may dispatch on different threads in the Strands framework.
Includes an after_invocation cleanup hook as a safety net against leaked
semaphores.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from strands.hooks.events import AfterInvocationEvent, AfterToolCallEvent, BeforeToolCallEvent
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
    """Acquires/releases per-group semaphores around tool calls.

    Uses thread-local storage to track which semaphore was acquired,
    avoiding the thread-ID mismatch issue where before/after hooks
    may fire on different threads.
    """

    name = "rate_limit"

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        effective = {**_DEFAULT_LIMITS, **(limits or {})}
        self._semaphores: dict[str, threading.Semaphore] = {
            group: threading.Semaphore(count) for group, count in effective.items()
        }
        self._local = threading.local()
        self._lock = threading.Lock()
        self._active_count: dict[str, int] = {group: 0 for group in effective}
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
            self._local.acquired_group = group
            self._local.start_time = time.monotonic()
            with self._lock:
                self._active_count[group] = self._active_count.get(group, 0) + 1
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
            start_time = getattr(self._local, "start_time", 0.0)
            self._local.start_time = 0.0
            self._local.acquired_group = None
            with self._lock:
                self._active_count[group] = max(0, self._active_count.get(group, 1) - 1)
            sem.release()
            elapsed = time.monotonic() - start_time if start_time else 0.0
            logger.debug(
                "tool=<%s>, group=<%s>, elapsed_ms=<%d> | released rate-limit semaphore",
                tool_name,
                group,
                int(elapsed * 1000),
            )

    @hook
    def after_invocation(self, event: AfterInvocationEvent) -> None:
        """Safety net: release any semaphore still held after invocation ends.

        This catches edge cases where after_tool_call was somehow skipped
        (framework bug, thread crash, etc.) to prevent permanent deadlocks.
        """
        acquired = getattr(self._local, "acquired_group", None)
        if acquired:
            sem = self._semaphores.get(acquired)
            if sem:
                logger.warning(
                    "group=<%s> | releasing leaked semaphore in after_invocation safety net",
                    acquired,
                )
                self._local.acquired_group = None
                self._local.start_time = 0.0
                with self._lock:
                    self._active_count[acquired] = max(
                        0, self._active_count.get(acquired, 1) - 1
                    )
                sem.release()
