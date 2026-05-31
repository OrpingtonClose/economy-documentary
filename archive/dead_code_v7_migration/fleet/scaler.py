"""
Fleet scaler — multi-VM provisioning and rolling scale-down.

Calculates optimal worker count based on workload and budget,
provisions VMs in parallel, and scales down as the clip queue drains.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Optional

from fleet.cost_tracker import BOOT_OVERHEAD_HOURS, DEFAULT_PRICE_PER_HOUR, CostTracker

if TYPE_CHECKING:
    from fleet.work_queue import WorkQueue

logger = logging.getLogger(__name__)

# Hard cap on parallel video workers
MAX_PARALLEL_VIDEO_WORKERS = int(os.environ.get("MAX_VIDEO_WORKERS", "5"))
# Don't provision a VM for fewer than this many clips
MIN_CLIPS_PER_WORKER = int(os.environ.get("MIN_CLIPS_PER_WORKER", "5"))
# Average generation time per clip (seconds) — used for estimates
AVG_GEN_TIME_SEC = float(os.environ.get("AVG_CLIP_GEN_TIME", "180"))


def calculate_optimal_workers(
    num_clips: int,
    budget_ceiling: float = 0.0,
    avg_clip_duration_sec: float = AVG_GEN_TIME_SEC,
    max_workers: int = MAX_PARALLEL_VIDEO_WORKERS,
    min_clips_per_worker: int = MIN_CLIPS_PER_WORKER,
    price_per_hour: float = DEFAULT_PRICE_PER_HOUR,
) -> int:
    """Determine how many video VMs to provision.

    Returns at least 1, at most *max_workers*.

    The total GPU cost is roughly constant regardless of N (same total
    work). The variable is per-VM boot overhead (~15 min billed).
    So cost ≈ total_gpu_hours × price + N × boot_overhead × price.
    """
    if num_clips <= 0:
        return 1

    # 1. Work-based limit: don't provision more workers than useful
    work_limit = max(1, num_clips // min_clips_per_worker)

    # 2. Budget-based limit
    budget_limit = max_workers
    if budget_ceiling > 0:
        total_gpu_hours = (num_clips * avg_clip_duration_sec) / 3600
        for n in range(max_workers, 0, -1):
            total_cost = (
                (total_gpu_hours + n * BOOT_OVERHEAD_HOURS) * price_per_hour
            )
            if total_cost <= budget_ceiling * 0.9:  # 10% buffer
                budget_limit = n
                break
        else:
            budget_limit = 1

    # 3. Hard cap
    optimal = min(work_limit, budget_limit, max_workers)
    return max(1, optimal)


def check_scale_down(
    active_workers: int,
    clips_remaining: int,
    clips_in_progress: int,
) -> int:
    """Return number of workers to release (0 = no change).

    Scale down when workers would be idle after their current clip:
    if remaining work < active workers, surplus workers have nothing
    to do. Keep at least 1 buffer worker for retries.
    """
    if active_workers <= 1:
        return 0

    work_left = clips_remaining + clips_in_progress
    idle_after_current = max(0, active_workers - work_left)

    # Keep at least 1 buffer worker
    workers_to_release = max(0, idle_after_current - 1)
    return workers_to_release


class FleetScaler:
    """Manages provisioning and scale-down of video VMs.

    Works with the existing WorkerProvisioner infrastructure but adds
    multi-VM support.

    Usage::

        scaler = FleetScaler(cost_tracker=tracker, work_queue=queue)
        # At production start:
        n = scaler.provision_fleet(num_clips=60, budget=15.0)
        # Periodically during production:
        released = scaler.check_and_scale_down()
        # At production end:
        scaler.destroy_all()
    """

    def __init__(
        self,
        cost_tracker: CostTracker,
        work_queue: Optional[WorkQueue] = None,
    ) -> None:
        self._cost_tracker = cost_tracker
        self._work_queue = work_queue
        self._lock = threading.Lock()
        self._active_vms: dict[str, dict] = {}  # vm_id → {url, role, ...}
        self._provisioning_remaining = 0  # threads still running

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    def provision_fleet(
        self,
        num_clips: int,
        budget_ceiling: float = 0.0,
    ) -> int:
        """Provision optimal number of video VMs.

        Calculates how many VMs to start, provisions them in parallel
        via the existing WorkerProvisioner, and returns the target count.

        The actual provisioning is async — VMs register themselves with
        InfraAgent as they come online. This method returns immediately
        with the target count.
        """
        optimal = calculate_optimal_workers(
            num_clips=num_clips,
            budget_ceiling=budget_ceiling,
        )

        logger.info(
            "FleetScaler: provisioning %d video VMs for %d clips (budget=$%.2f)",
            optimal, num_clips, budget_ceiling,
        )

        # Register projected cost
        total_gpu_hours = (num_clips * AVG_GEN_TIME_SEC) / 3600
        projected = (
            (total_gpu_hours + optimal * BOOT_OVERHEAD_HOURS) * DEFAULT_PRICE_PER_HOUR
        )
        self._cost_tracker.set_projection(projected)

        # Use existing WorkerProvisioner for each VM
        self._provision_n_vms(optimal)

        return optimal

    def _provision_n_vms(self, count: int) -> None:
        """Provision N video VMs using existing infrastructure.

        Uses the WorkerProvisioner pattern: each VM goes through
        Vast.ai provisioning → SSH tunnel → model download → health check.
        """
        logger.warning("FleetScaler: WorkerProvisioner removed — no-op")

        with self._lock:
            self._provisioning_remaining = count

        threads: list[threading.Thread] = []
        for i in range(count):
            t = threading.Thread(
                target=self._provision_one_vm,
                args=(i,),
                name=f"fleet-provision-{i}",
                daemon=True,
            )
            threads.append(t)
            t.start()

        # Don't wait — VMs register themselves via InfraAgent.add_worker()
        # as they become healthy. The work queue will dispatch to them
        # as they come online.
        logger.info(
            "FleetScaler: %d provisioning threads launched", len(threads)
        )

    def _provision_one_vm(self, index: int) -> None:
        """Provision a single video VM (runs in a thread)."""
        vm_id = f"video-{index}-{int(time.time())}"
        logger.warning("FleetScaler: WorkerProvisioner removed — no-op for %s", vm_id)
        with self._lock:
            self._provisioning_remaining = max(0, self._provisioning_remaining - 1)

    # ------------------------------------------------------------------
    # Scale-down
    # ------------------------------------------------------------------

    def check_and_scale_down(self) -> int:
        """Check if workers should be released and release them.

        Returns number of workers released.
        """
        if not self._work_queue:
            return 0

        with self._lock:
            active_count = len(self._active_vms)

        if active_count <= 1:
            return 0

        to_release = check_scale_down(
            active_workers=active_count,
            clips_remaining=self._work_queue.pending_count,
            clips_in_progress=self._work_queue.in_progress_count,
        )

        if to_release <= 0:
            return 0

        # Also check budget — if over budget, release more aggressively
        if self._cost_tracker.should_stop_provisioning():
            to_release = max(to_release, active_count - 1)
            logger.warning(
                "FleetScaler: budget nearly exhausted — scaling to 1 worker"
            )

        released = 0
        with self._lock:
            # Prefer releasing idle VMs — cross-reference with work queue
            # to find which VMs have clips currently assigned/generating.
            busy_workers: set[str] = set()
            if self._work_queue:
                for clip in self._work_queue.get_in_progress_clips():
                    if clip.assigned_to:
                        busy_workers.add(clip.assigned_to)

            # Sort: idle VMs first (not in busy_workers), then by ID
            vm_ids = sorted(
                self._active_vms.keys(),
                key=lambda vid: (
                    # VMs whose URL matches a busy worker sort last
                    1 if self._active_vms[vid].get("url", "") in busy_workers else 0,
                    vid,
                ),
            )

        for vm_id in vm_ids[:to_release]:
            self._release_vm(vm_id)
            released += 1

        if released > 0:
            logger.info(
                "FleetScaler: released %d VMs (active: %d → %d)",
                released, active_count, active_count - released,
            )

        return released

    def _release_vm(self, vm_id: str) -> None:
        """Gracefully release a VM: unregister, stop cost, destroy."""
        with self._lock:
            vm_info = self._active_vms.pop(vm_id, None)

        if not vm_info:
            return

        url = vm_info.get("url", "")

        # Stop cost tracking
        self._cost_tracker.stop_vm(vm_id)

        # Unregister from InfraAgent
        try:
            from infra_agent import get_infra_agent
            agent = get_infra_agent()
            if agent:
                agent.remove_worker(url)
        except Exception as e:
            logger.warning("FleetScaler: could not unregister VM %s: %s", vm_id, e)

        # Destroy the Vast.ai VM
        logger.warning("FleetScaler: WorkerProvisioner removed — cannot destroy VM %s", vm_id)

        logger.info("FleetScaler: released VM %s (%s)", vm_id, url)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def destroy_all(self) -> None:
        """Destroy all managed VMs. Called at production end."""
        with self._lock:
            vm_ids = list(self._active_vms.keys())

        for vm_id in vm_ids:
            self._release_vm(vm_id)

        logger.info("FleetScaler: all VMs destroyed")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_active_vm_ids(self) -> list[str]:
        with self._lock:
            return list(self._active_vms.keys())

    def get_summary(self) -> dict:
        with self._lock:
            vms = dict(self._active_vms)
        return {
            "active_vms": len(vms),
            "vm_ids": list(vms.keys()),
            "provisioning": self._provisioning_remaining > 0,
            "provisioning_remaining": self._provisioning_remaining,
            "cost": self._cost_tracker.summary(),
        }
