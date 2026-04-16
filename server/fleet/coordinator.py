"""
Fleet Coordinator — orchestrates work dispatch, scaling, and health.

Hybrid approach:
- Deterministic for routine dispatch (fast, predictable)
- Escalation for anomaly response (fleet-level patterns)

Sits between the ProductionOrchestrator (which decides WHAT clips to make)
and the VMAgents (which generate clips on GPUs).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

from fleet.cost_tracker import CostTracker
from fleet.scaler import FleetScaler
from fleet.systemic_detector import SystemicDetector
from fleet.work_queue import QueuedClip, WorkQueue

logger = logging.getLogger(__name__)

# How often the coordinator checks for scale-down and systemic issues (seconds)
COORDINATOR_POLL_INTERVAL = float(os.environ.get("FLEET_POLL_INTERVAL", "30"))


class FleetCoordinator:
    """Central fleet coordination: dispatch, scaling, health, cost.

    Usage::

        coordinator = FleetCoordinator(budget_ceiling=15.0)
        coordinator.enqueue_clips(clips)
        coordinator.start()

        # Workers pull work:
        clip = coordinator.pull_work(worker_id="vm-abc")

        # Report results:
        coordinator.report_completed(clip_id, output_path, gen_time)
        coordinator.report_failed(clip_id, worker_id, error)

        # Wait for all clips:
        await coordinator.wait_for_completion()

        # Get results:
        results = coordinator.get_results()

        coordinator.shutdown()
    """

    def __init__(
        self,
        budget_ceiling: float = 0.0,
        clip_timeout: float = WorkQueue.DEFAULT_TIMEOUT,
    ) -> None:
        self._cost_tracker = CostTracker(budget_ceiling=budget_ceiling)
        self._queue = WorkQueue(clip_timeout=clip_timeout)
        self._scaler = FleetScaler(
            cost_tracker=self._cost_tracker,
            work_queue=self._queue,
        )
        self._detector = SystemicDetector(
            cost_tracker=self._cost_tracker,
        )
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._completion_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._paused = False
        self._pause_reason = ""

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def enqueue_clips(self, clips: list[QueuedClip]) -> int:
        """Add clips to the work queue. Returns number enqueued."""
        return self._queue.enqueue_batch(clips)

    def enqueue_clip(self, clip: QueuedClip) -> None:
        """Add a single clip to the work queue."""
        self._queue.enqueue(clip)

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    def provision_fleet(
        self,
        num_clips: int,
        budget_ceiling: float = 0.0,
    ) -> int:
        """Provision optimal number of VMs for the workload."""
        if budget_ceiling > 0:
            self._cost_tracker._budget_ceiling = budget_ceiling
        return self._scaler.provision_fleet(num_clips, budget_ceiling)

    # ------------------------------------------------------------------
    # Work dispatch (pull model)
    # ------------------------------------------------------------------

    def pull_work(self, worker_id: str) -> Optional[QueuedClip]:
        """Called by a worker (or dispatcher) to get the next clip.

        Returns None if no work is available or if the fleet is paused.
        """
        if self._paused:
            return None
        return self._queue.pull_work(worker_id)

    # ------------------------------------------------------------------
    # Health-aware dispatch (push model)
    # ------------------------------------------------------------------

    def dispatch_to_healthy_worker(self) -> Optional[tuple[str, QueuedClip]]:
        """Pick a healthy worker and assign it the next clip.

        Returns (worker_url, clip) or None if no work/workers available.
        Uses InfraAgent.get_healthy_workers() instead of blind round-robin.
        """
        if self._paused:
            return None

        try:
            from infra_agent import WorkerRole, get_infra_agent
            agent = get_infra_agent()
            if not agent:
                return None

            healthy = agent.get_healthy_workers(role=WorkerRole.VIDEO)
            if not healthy:
                logger.debug("FleetCoordinator: no healthy video workers available")
                return None

            # Try each healthy worker (least-recently-used first)
            for worker_url in healthy:
                clip = self._queue.pull_work(worker_id=worker_url)
                if clip:
                    return (worker_url, clip)

            return None
        except ImportError:
            return None

    # ------------------------------------------------------------------
    # Result reporting
    # ------------------------------------------------------------------

    def report_completed(
        self,
        clip_id: str,
        output_path: str = "",
        gen_time: float = 0.0,
        qa_quality: str = "",
        qa_reason: str = "",
        worker_id: str = "",
    ) -> None:
        """Report a clip as successfully completed."""
        self._queue.mark_completed(
            clip_id=clip_id,
            output_path=output_path,
            gen_time=gen_time,
            qa_quality=qa_quality,
            qa_reason=qa_reason,
        )
        if worker_id and gen_time > 0:
            self._detector.record_generation(worker_id, gen_time, clip_id)

        # Check if all done
        if self._queue.is_complete():
            self._completion_event.set()

    def report_failed(
        self,
        clip_id: str,
        worker_id: str,
        error: str,
        category: str = "unknown",
    ) -> None:
        """Report a clip generation failure."""
        self._queue.mark_failed(
            clip_id=clip_id,
            worker_id=worker_id,
            error=error,
            category=category,
        )
        self._detector.record_failure(worker_id, error, category)

        # Check if all done (might have dead-lettered the last clip)
        if self._queue.is_complete():
            self._completion_event.set()

    # ------------------------------------------------------------------
    # Wait for completion
    # ------------------------------------------------------------------

    async def wait_for_completion(self, timeout: float = 7200.0) -> bool:
        """Wait until all clips are completed or dead-lettered.

        Returns True if completed, False if timed out.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._completion_event.wait, timeout
        )

    def wait_for_completion_sync(self, timeout: float = 7200.0) -> bool:
        """Synchronous version of wait_for_completion."""
        return self._completion_event.wait(timeout)

    # ------------------------------------------------------------------
    # Background monitoring loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background monitoring thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown_event.clear()
        self._completion_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="fleet-coordinator",
            daemon=True,
        )
        self._thread.start()

    def _monitor_loop(self) -> None:
        """Background loop: check scale-down, systemic patterns, completion."""
        logger.info("FleetCoordinator: monitoring started")
        while not self._shutdown_event.is_set():
            if self._shutdown_event.wait(timeout=COORDINATOR_POLL_INTERVAL):
                break

            # Check systemic patterns
            queue_clips = self._queue.get_all_clips()
            patterns = self._detector.check_patterns(queue_clips=queue_clips)
            self._handle_patterns(patterns)

            # Check scale-down
            self._scaler.check_and_scale_down()

            # Check completion
            if self._queue.is_complete():
                self._completion_event.set()

        logger.info("FleetCoordinator: monitoring stopped")

    def _handle_patterns(self, patterns: list) -> None:
        """Respond to detected systemic patterns (Level 1 auto-response)."""
        for pattern in patterns:
            if pattern.pattern_type == "cascade_failure":
                self._pause(
                    f"Cascade failure detected: {pattern.hypothesis}"
                )
            elif pattern.pattern_type == "common_error":
                logger.critical(
                    "FleetCoordinator: common error across fleet — %s",
                    pattern.hypothesis,
                )
            elif pattern.pattern_type == "budget_burn":
                logger.warning(
                    "FleetCoordinator: budget burn anomaly — %s",
                    pattern.hypothesis,
                )
                # Aggressive scale-down
                self._scaler.check_and_scale_down()

    def _pause(self, reason: str) -> None:
        """Pause the fleet (stop dispatching, stop provisioning)."""
        with self._lock:
            if self._paused:
                return
            self._paused = True
            self._pause_reason = reason
        logger.critical("FleetCoordinator: PAUSED — %s", reason)

    def resume(self) -> None:
        """Resume the fleet after a pause."""
        with self._lock:
            self._paused = False
            self._pause_reason = ""
        logger.info("FleetCoordinator: RESUMED")

    # ------------------------------------------------------------------
    # Shutdown / cleanup
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop monitoring and destroy all VMs."""
        self._shutdown_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._scaler.destroy_all()
        logger.info("FleetCoordinator: shutdown complete")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def queue(self) -> WorkQueue:
        return self._queue

    @property
    def cost_tracker(self) -> CostTracker:
        return self._cost_tracker

    @property
    def systemic_detector(self) -> SystemicDetector:
        return self._detector

    def get_summary(self) -> dict:
        """Return a comprehensive fleet status summary."""
        return {
            "queue": self._queue.get_summary(),
            "scaler": self._scaler.get_summary(),
            "patterns": [
                {
                    "type": p.pattern_type,
                    "severity": p.severity,
                    "hypothesis": p.hypothesis,
                }
                for p in self._detector.get_active_patterns()
            ],
            "paused": self._paused,
            "pause_reason": self._pause_reason,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_coordinator: Optional[FleetCoordinator] = None
_coord_lock = threading.Lock()


def get_fleet_coordinator() -> Optional[FleetCoordinator]:
    """Return the global FleetCoordinator singleton, or None."""
    return _coordinator


def create_fleet_coordinator(
    budget_ceiling: float = 0.0,
    clip_timeout: float = WorkQueue.DEFAULT_TIMEOUT,
) -> FleetCoordinator:
    """Create and return the global FleetCoordinator singleton."""
    global _coordinator
    with _coord_lock:
        if _coordinator is not None:
            return _coordinator
        _coordinator = FleetCoordinator(
            budget_ceiling=budget_ceiling,
            clip_timeout=clip_timeout,
        )
    return _coordinator


def reset_fleet_coordinator() -> None:
    """Reset the global coordinator (for testing or between production runs)."""
    global _coordinator
    with _coord_lock:
        if _coordinator is not None:
            _coordinator.shutdown()
        _coordinator = None
