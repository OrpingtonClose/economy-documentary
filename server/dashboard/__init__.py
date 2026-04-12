"""
Dashboard -- dual-storage design for active pipeline collectors.

Ported from MiroThinker. Provides per-request isolation via ContextVar
and shared registry for dashboard SSE streaming.
"""

from __future__ import annotations

import contextvars
import threading
from typing import Optional

# Per-request collector (set by middleware for each AG-UI request)
_active_collector: contextvars.ContextVar[Optional["PipelineCollector"]] = (
    contextvars.ContextVar("_active_collector", default=None)
)

# Shared registry of all active collectors (for dashboard SSE)
_all_collectors: dict[str, "PipelineCollector"] = {}
_registry_lock = threading.Lock()


def set_active_collector(collector: "PipelineCollector") -> None:
    """Set the active collector for the current async context."""
    _active_collector.set(collector)
    with _registry_lock:
        _all_collectors[collector.run_id] = collector


def get_active_collector() -> Optional["PipelineCollector"]:
    """Get the active collector for the current async context."""
    return _active_collector.get()


def get_any_active_collector() -> Optional["PipelineCollector"]:
    """Get the most recent active collector (for dashboard endpoints).

    Returns the latest collector by insertion order so the dashboard
    always shows the newest pipeline run, not a stale completed one.
    """
    c = _active_collector.get()
    if c is not None:
        return c
    with _registry_lock:
        if _all_collectors:
            # Python 3.7+ dicts preserve insertion order — return last
            return list(_all_collectors.values())[-1]
    return None


def get_all_active_collectors() -> dict[str, "PipelineCollector"]:
    """Get all active collectors (for dashboard multi-run view)."""
    with _registry_lock:
        return dict(_all_collectors)


def remove_collector(run_id: str) -> None:
    """Remove a collector from the shared registry."""
    with _registry_lock:
        _all_collectors.pop(run_id, None)
