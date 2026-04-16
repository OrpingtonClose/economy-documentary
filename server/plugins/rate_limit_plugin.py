"""Rate-limit plugin -- GPU / TTS / VastAI semaphores.

Replaces server/callbacks/before_tool.py and server/callbacks/after_tool.py.

Uses per-invocation tracking keyed by toolUseId (not thread-local) because
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

_ACQUIRE_TIMEOUT = 120  # seconds — matches the old before_tool.py timeout


class RateLimitPlugin(Plugin):
    """Acquires/releases per-group semaphores around tool calls.

    Uses per-invocation dict keyed by toolUseId to track which semaphores
    are held, avoiding thread-local issues where before/after hooks may
    fire on different threads.
    """

    name = "rate_limit"

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        effective = {**_DEFAULT_LIMITS, **(limits or {})}
        self._semaphores: dict[str, threading.Semaphore] = {
            group: threading.Semaphore(count) for group, count in effective.items()
        }
        self._lock = threading.Lock()
        # Track acquired semaphores by toolUseId → (group, start_time)
        self._active: dict[str, tuple[str, float]] = {}
        self._active_count: dict[str, int] = {group: 0 for group in effective}
        super().__init__()

    def _group_for(self, tool_name: str) -> str | None:
        return _TOOL_GROUPS.get(tool_name)

    @hook
    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        """Acquire semaphore for the tool's resource group with timeout."""
        tool_name = event.tool_use.get("name", "")
        group = self._group_for(tool_name)
        if not group:
            return

        sem = self._semaphores.get(group)
        if sem:
            acquired = sem.acquire(blocking=True, timeout=_ACQUIRE_TIMEOUT)
            if not acquired:
                error_msg = (
                    f"Rate limit timeout: could not acquire {group} semaphore "
                    f"after {_ACQUIRE_TIMEOUT}s. Resource group is saturated."
                )
                logger.error(
                    "tool=<%s>, group=<%s> | rate-limit acquire timed out after %ds",
                    tool_name,
                    group,
                    _ACQUIRE_TIMEOUT,
                )
                raise RuntimeError(error_msg)

            tool_use_id = event.tool_use.get("toolUseId", "")
            with self._lock:
                if tool_use_id:
                    self._active[tool_use_id] = (group, time.monotonic())
                self._active_count[group] = self._active_count.get(group, 0) + 1
            logger.debug(
                "tool=<%s>, group=<%s>, tool_use_id=<%s> | acquired rate-limit semaphore",
                tool_name,
                group,
                tool_use_id,
            )

    @hook
    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        """Release semaphore for the tool's resource group."""
        tool_name = event.tool_use.get("name", "")
        group = self._group_for(tool_name)
        if not group:
            return

        tool_use_id = event.tool_use.get("toolUseId", "")
        sem = self._semaphores.get(group)
        if not sem:
            return

        # Only release if we actually acquired for this tool_use_id
        with self._lock:
            if tool_use_id and tool_use_id in self._active:
                acquired_group, start_time = self._active.pop(tool_use_id)
                self._active_count[acquired_group] = max(
                    0, self._active_count.get(acquired_group, 1) - 1
                )
                sem.release()
                elapsed = time.monotonic() - start_time
                logger.debug(
                    "tool=<%s>, group=<%s>, elapsed_ms=<%d> | released rate-limit semaphore",
                    tool_name,
                    acquired_group,
                    int(elapsed * 1000),
                )
            elif not tool_use_id:
                # Fallback for missing toolUseId — release based on group name
                self._active_count[group] = max(
                    0, self._active_count.get(group, 1) - 1
                )
                sem.release()
                logger.debug(
                    "tool=<%s>, group=<%s> | released rate-limit semaphore (no toolUseId)",
                    tool_name,
                    group,
                )

    @hook
    def after_invocation(self, event: AfterInvocationEvent) -> None:
        """Safety net: release any semaphores still held after invocation ends."""
        with self._lock:
            leaked = list(self._active.items())
            for tool_use_id, (group, _start) in leaked:
                sem = self._semaphores.get(group)
                if sem:
                    logger.warning(
                        "tool_use_id=<%s>, group=<%s> | releasing leaked semaphore in after_invocation",
                        tool_use_id,
                        group,
                    )
                    self._active_count[group] = max(
                        0, self._active_count.get(group, 1) - 1
                    )
                    sem.release()
            self._active.clear()
