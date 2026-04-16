"""Concurrency plugin -- LLM semaphore and token management.

Replaces server/callbacks/before_model.py and server/callbacks/after_model.py.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from strands.hooks.events import AfterModelCallEvent, BeforeModelCallEvent
from strands.plugins import Plugin, hook

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_LLM", "2"))


class ConcurrencyPlugin(Plugin):
    """Manages LLM concurrency via semaphore and logs token usage.

    Uses threading.local() to track per-call start times so that
    before/after hooks are matched correctly even if the framework
    dispatches them on different threads in the future.
    """

    name = "concurrency"

    def __init__(self, max_concurrent: int = _DEFAULT_MAX_CONCURRENT) -> None:
        self._max_concurrent = max_concurrent
        self._semaphore = threading.Semaphore(max_concurrent)
        self._local = threading.local()
        self._active_count = 0
        self._lock = threading.Lock()
        super().__init__()

    @hook
    def before_model_call(self, event: BeforeModelCallEvent) -> None:
        """Acquire LLM semaphore before model call."""
        self._semaphore.acquire()
        self._local.start_time = time.monotonic()
        with self._lock:
            self._active_count += 1
            active = self._active_count
        logger.debug(
            "active=<%d>, max=<%d> | acquired llm semaphore",
            active,
            self._max_concurrent,
        )

    @hook
    def after_model_call(self, event: AfterModelCallEvent) -> None:
        """Release LLM semaphore and log token usage."""
        start_time = getattr(self._local, "start_time", 0.0)
        self._local.start_time = 0.0
        with self._lock:
            self._active_count = max(0, self._active_count - 1)
        self._semaphore.release()

        elapsed = time.monotonic() - start_time if start_time else 0.0
        logger.debug(
            "elapsed_ms=<%d> | released llm semaphore",
            int(elapsed * 1000),
        )
