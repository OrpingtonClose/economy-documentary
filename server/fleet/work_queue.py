"""
Fleet work queue — persistent clip queue with retry-on-different-worker.

Lifecycle::

    pending ──assign──▶ assigned ──start──▶ generating ──done──▶ completed
       ▲                    │                    │
       │                    │ timeout            │ fail
       │                    ▼                    ▼
       └──────────── failed (if attempts < max) ─┘
                            │
                            │ attempts >= max
                            ▼
                        dead_letter (escalate)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ClipStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


@dataclass
class QueuedClip:
    """A single clip in the work queue."""

    clip_id: str
    scene_num: int
    phrase_idx: int
    prompt: str
    negative_prompt: str
    duration: float
    lora_id: str
    lora_weight: float
    priority: int = 0
    status: ClipStatus = ClipStatus.PENDING
    assigned_to: str = ""
    assigned_at: float = 0.0
    attempts: int = 0
    max_attempts: int = 3
    error_history: list[dict] = field(default_factory=list)
    completed_at: float = 0.0
    gen_time: float = 0.0
    output_path: str = ""
    qa_quality: str = ""
    qa_reason: str = ""

    @property
    def failed_worker_ids(self) -> set[str]:
        """Workers that already failed on this clip."""
        return {e.get("worker_id", "") for e in self.error_history if e.get("worker_id")}


class WorkQueue:
    """Thread-safe work queue for video clip generation.

    Supports:
    - Priority ordering (higher = more urgent; retries get +10)
    - Retry on different worker (never retries on same worker)
    - Timeout detection for stuck clips
    - Dead letter queue for poison clips
    """

    DEFAULT_TIMEOUT = 600.0  # 10 min — 2× typical 5 min gen time

    def __init__(self, clip_timeout: float = DEFAULT_TIMEOUT) -> None:
        self._lock = threading.Lock()
        self._clips: dict[str, QueuedClip] = {}
        self._clip_timeout = clip_timeout

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def enqueue(self, clip: QueuedClip) -> None:
        """Add a clip to the queue."""
        with self._lock:
            if clip.clip_id in self._clips:
                logger.warning("WorkQueue: clip %s already queued — skipping", clip.clip_id)
                return
            clip.status = ClipStatus.PENDING
            self._clips[clip.clip_id] = clip
        logger.debug("WorkQueue: enqueued %s (priority=%d)", clip.clip_id, clip.priority)

    def enqueue_batch(self, clips: list[QueuedClip]) -> int:
        """Add multiple clips. Returns number actually enqueued."""
        count = 0
        for clip in clips:
            with self._lock:
                if clip.clip_id not in self._clips:
                    clip.status = ClipStatus.PENDING
                    self._clips[clip.clip_id] = clip
                    count += 1
        logger.info("WorkQueue: enqueued %d clips (batch of %d)", count, len(clips))
        return count

    # ------------------------------------------------------------------
    # Pull work (called by workers or dispatcher)
    # ------------------------------------------------------------------

    def pull_work(self, worker_id: str) -> Optional[QueuedClip]:
        """Get the highest-priority pending clip that hasn't failed on this worker.

        Returns None if no suitable work is available.
        """
        with self._lock:
            # Reclaim timed-out clips first
            self._reclaim_timed_out()

            candidates = [
                c for c in self._clips.values()
                if c.status == ClipStatus.PENDING
                and worker_id not in c.failed_worker_ids
            ]
            if not candidates:
                return None

            # Sort by priority (descending), then scene order
            candidates.sort(
                key=lambda c: (-c.priority, c.scene_num, c.phrase_idx)
            )
            clip = candidates[0]
            clip.status = ClipStatus.ASSIGNED
            clip.assigned_to = worker_id
            clip.assigned_at = time.time()
            clip.attempts += 1

        logger.debug(
            "WorkQueue: assigned %s to %s (attempt %d, priority %d)",
            clip.clip_id, worker_id, clip.attempts, clip.priority,
        )
        return clip

    def mark_generating(self, clip_id: str) -> None:
        """Mark a clip as actively generating."""
        with self._lock:
            clip = self._clips.get(clip_id)
            if clip and clip.status == ClipStatus.ASSIGNED:
                clip.status = ClipStatus.GENERATING

    # ------------------------------------------------------------------
    # Completion / failure
    # ------------------------------------------------------------------

    def mark_completed(
        self,
        clip_id: str,
        output_path: str = "",
        gen_time: float = 0.0,
        qa_quality: str = "",
        qa_reason: str = "",
    ) -> None:
        """Mark a clip as successfully completed."""
        with self._lock:
            clip = self._clips.get(clip_id)
            if not clip:
                return
            clip.status = ClipStatus.COMPLETED
            clip.completed_at = time.time()
            clip.output_path = output_path
            clip.gen_time = gen_time
            clip.qa_quality = qa_quality
            clip.qa_reason = qa_reason

        logger.debug("WorkQueue: completed %s (gen_time=%.1fs, qa=%s)", clip_id, gen_time, qa_quality)

    def mark_failed(
        self,
        clip_id: str,
        worker_id: str,
        error: str,
        category: str = "unknown",
    ) -> None:
        """Mark a clip as failed. Re-queues if attempts remain, else dead-letters."""
        with self._lock:
            clip = self._clips.get(clip_id)
            if not clip:
                return

            clip.error_history.append({
                "worker_id": worker_id,
                "error": error,
                "category": category,
                "timestamp": time.time(),
                "attempt": clip.attempts,
            })

            if clip.attempts >= clip.max_attempts:
                clip.status = ClipStatus.DEAD_LETTER
                logger.warning(
                    "WorkQueue: %s dead-lettered after %d attempts (last error: %s)",
                    clip_id, clip.attempts, error[:200],
                )
            else:
                # Re-queue with higher priority
                clip.status = ClipStatus.PENDING
                clip.assigned_to = ""
                clip.assigned_at = 0.0
                clip.priority += 10  # boost priority for retries
                logger.info(
                    "WorkQueue: %s re-queued (attempt %d/%d, priority now %d)",
                    clip_id, clip.attempts, clip.max_attempts, clip.priority,
                )

    # ------------------------------------------------------------------
    # Timeout reclamation
    # ------------------------------------------------------------------

    def _reclaim_timed_out(self) -> None:
        """Reclaim clips stuck in assigned/generating state. Must hold lock."""
        now = time.time()
        for clip in self._clips.values():
            if clip.status in (ClipStatus.ASSIGNED, ClipStatus.GENERATING):
                if clip.assigned_at > 0 and (now - clip.assigned_at) > self._clip_timeout:
                    old_worker = clip.assigned_to
                    clip.error_history.append({
                        "worker_id": old_worker,
                        "error": f"Timeout after {self._clip_timeout:.0f}s",
                        "category": "timeout",
                        "timestamp": now,
                        "attempt": clip.attempts,
                    })
                    if clip.attempts >= clip.max_attempts:
                        clip.status = ClipStatus.DEAD_LETTER
                        logger.warning(
                            "WorkQueue: %s timed out and dead-lettered (worker %s)",
                            clip.clip_id, old_worker,
                        )
                    else:
                        clip.status = ClipStatus.PENDING
                        clip.assigned_to = ""
                        clip.assigned_at = 0.0
                        clip.priority += 10
                        logger.warning(
                            "WorkQueue: %s timed out on %s — re-queued (attempt %d/%d)",
                            clip.clip_id, old_worker, clip.attempts, clip.max_attempts,
                        )

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------

    @property
    def depth(self) -> int:
        """Number of clips still needing work (pending + assigned + generating)."""
        with self._lock:
            return sum(
                1 for c in self._clips.values()
                if c.status in (ClipStatus.PENDING, ClipStatus.ASSIGNED, ClipStatus.GENERATING)
            )

    @property
    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for c in self._clips.values() if c.status == ClipStatus.PENDING)

    @property
    def in_progress_count(self) -> int:
        with self._lock:
            return sum(
                1 for c in self._clips.values()
                if c.status in (ClipStatus.ASSIGNED, ClipStatus.GENERATING)
            )

    @property
    def completed_count(self) -> int:
        with self._lock:
            return sum(1 for c in self._clips.values() if c.status == ClipStatus.COMPLETED)

    @property
    def dead_letter_count(self) -> int:
        with self._lock:
            return sum(1 for c in self._clips.values() if c.status == ClipStatus.DEAD_LETTER)

    @property
    def total_count(self) -> int:
        with self._lock:
            return len(self._clips)

    def get_completed_clips(self) -> list[QueuedClip]:
        """Return all completed clips in scene order."""
        with self._lock:
            completed = [
                c for c in self._clips.values()
                if c.status == ClipStatus.COMPLETED
            ]
        completed.sort(key=lambda c: (c.scene_num, c.phrase_idx))
        return completed

    def get_dead_letter_clips(self) -> list[QueuedClip]:
        """Return all dead-lettered clips."""
        with self._lock:
            return [c for c in self._clips.values() if c.status == ClipStatus.DEAD_LETTER]

    def get_all_clips(self) -> list[QueuedClip]:
        """Return all clips in scene order."""
        with self._lock:
            clips = list(self._clips.values())
        clips.sort(key=lambda c: (c.scene_num, c.phrase_idx))
        return clips

    def get_summary(self) -> dict:
        """Return a summary of queue state."""
        with self._lock:
            by_status: dict[str, int] = {}
            total_gen_time = 0.0
            gen_count = 0
            for c in self._clips.values():
                by_status[c.status.value] = by_status.get(c.status.value, 0) + 1
                if c.status == ClipStatus.COMPLETED and c.gen_time > 0:
                    total_gen_time += c.gen_time
                    gen_count += 1

        return {
            "total": self.total_count,
            "by_status": by_status,
            "avg_gen_time": total_gen_time / max(gen_count, 1),
            "depth": self.depth,
        }

    def is_complete(self) -> bool:
        """True when all clips are completed or dead-lettered (no more work)."""
        with self._lock:
            if not self._clips:
                return False  # empty queue is not "complete"
            return all(
                c.status in (ClipStatus.COMPLETED, ClipStatus.DEAD_LETTER)
                for c in self._clips.values()
            )
