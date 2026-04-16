"""Concurrency plugin -- LLM semaphore and token management.

Replaces server/callbacks/before_model.py and server/callbacks/after_model.py.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from strands.hooks.events import AfterModelCallEvent, BeforeModelCallEvent
from strands.plugins import Plugin, hook

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CONCURRENT = 3


class ConcurrencyPlugin(Plugin):
    """Manages LLM concurrency via semaphore and logs token usage."""

    name = "concurrency"

    def __init__(self, max_concurrent: int = _DEFAULT_MAX_CONCURRENT) -> None:
        self._max_concurrent = max_concurrent
        self._semaphore = threading.Semaphore(max_concurrent)
        self._active_calls: dict[int, float] = {}
        self._lock = threading.Lock()
        super().__init__()

    @hook
    def before_model_call(self, event: BeforeModelCallEvent) -> None:
        """Acquire LLM semaphore before model call."""
        tid = threading.get_ident()
        self._semaphore.acquire()
        with self._lock:
            self._active_calls[tid] = time.monotonic()
        logger.debug(
            "thread=<%s>, active=<%d>, max=<%d> | acquired llm semaphore",
            tid,
            len(self._active_calls),
            self._max_concurrent,
        )

    @hook
    def after_model_call(self, event: AfterModelCallEvent) -> None:
        """Release LLM semaphore and log token usage."""
        tid = threading.get_ident()
        start_time = 0.0
        with self._lock:
            start_time = self._active_calls.pop(tid, 0.0)
        self._semaphore.release()

        elapsed = time.monotonic() - start_time if start_time else 0.0
        logger.debug(
            "thread=<%s>, elapsed_ms=<%d> | released llm semaphore",
            tid,
            int(elapsed * 1000),
        )
