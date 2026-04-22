"""RecoveryLogger — appends recovery decisions to a bounded in-memory log.

Used by the diagnostic classifier (component 12a) and the remanifestation
agent (component 12b) to record exactly one entry per recovery decision.
Written as a plain callable rather than a Strands ``HookProvider`` so
that unit tests can exercise the log semantics without spinning up an
LLM-backed agent.

Log shape::

    [
        {"agent": "classifier",    "artifact_id": "scene_01",
         "classification": "fixable",  "reasoning": "..."},
        {"agent": "remanifester",  "artifact_id": "scene_01",
         "changed_fields": ["prompt", "negative_prompt"]},
        ...
    ]

Exactly one entry per decision is the invariant enforced here; callers
drive that by calling :meth:`record_classification` /
:meth:`record_remanifestation` once per tool-call.
"""

from __future__ import annotations

import logging
from collections import deque
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


class RecoveryLogger:
    """Bounded, thread-safe recovery decision log.

    Args:
        max_entries: Cap on the number of entries kept; older entries
            are evicted FIFO. Defaults to 256 — large enough that a
            single pipeline run never evicts, small enough that a
            long-lived process can't leak memory via recovery storms.
    """

    def __init__(self, max_entries: int = 256) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._entries: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self._lock = Lock()

    def record_classification(
        self, artifact_id: str, classification: dict[str, Any]
    ) -> dict[str, Any]:
        """Record a classifier decision.

        Args:
            artifact_id: Artifact the decision is about.
            classification: Payload returned by
                :func:`recovery_agents.classify`; must include
                ``class``.

        Returns:
            The entry that was appended.
        """
        if not artifact_id:
            raise ValueError("artifact_id is required")
        if "class" not in classification:
            raise ValueError("classification must include 'class'")
        entry: dict[str, Any] = {
            "agent": "classifier",
            "artifact_id": artifact_id,
            "classification": classification["class"],
            "reasoning": classification.get("reasoning", ""),
            "signals": list(classification.get("signals", [])),
        }
        with self._lock:
            self._entries.append(entry)
        logger.debug(
            "agent=<classifier>, artifact_id=<%s>, class=<%s> | recovery decision",
            artifact_id,
            classification["class"],
        )
        return entry

    def record_remanifestation(
        self,
        artifact_id: str,
        diff: dict[str, Any],
    ) -> dict[str, Any]:
        """Record a remanifestation decision.

        Args:
            artifact_id: Artifact the revision is about.
            diff: Payload returned by
                :func:`recovery_agents.diff_concept`.

        Returns:
            The entry that was appended.
        """
        if not artifact_id:
            raise ValueError("artifact_id is required")
        entry: dict[str, Any] = {
            "agent": "remanifester",
            "artifact_id": artifact_id,
            "changed_fields": list(diff.get("changed_fields", [])),
        }
        with self._lock:
            self._entries.append(entry)
        logger.debug(
            "agent=<remanifester>, artifact_id=<%s>, changed=<%s> | recovery decision",
            artifact_id,
            entry["changed_fields"],
        )
        return entry

    def entries(self) -> list[dict[str, Any]]:
        """Return a snapshot of all recorded entries (oldest first)."""
        with self._lock:
            return list(self._entries)

    def count_for(self, artifact_id: str) -> int:
        """Return how many entries reference ``artifact_id``."""
        with self._lock:
            return sum(1 for e in self._entries if e.get("artifact_id") == artifact_id)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


__all__ = ["RecoveryLogger"]
