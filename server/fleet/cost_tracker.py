"""
Fleet cost tracking — real-time per-VM cost, budget ceiling enforcement.

Tracks cumulative spend across all VMs and gates provisioning decisions
against a hard budget ceiling.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Defaults (can be overridden via environment or config)
DEFAULT_PRICE_PER_HOUR = 3.00  # typical A100 80GB on Vast.ai
BOOT_OVERHEAD_HOURS = 0.25     # ~15 min boot/bootstrap
CREDIT_RESERVE = 2.00          # keep $2 reserve in account


@dataclass
class VMCost:
    """Cost tracking for a single VM."""

    vm_id: str
    price_per_hour: float
    started_at: float
    stopped_at: float = 0.0

    @property
    def hours_running(self) -> float:
        end = self.stopped_at or time.time()
        return (end - self.started_at) / 3600

    @property
    def cost_so_far(self) -> float:
        return self.hours_running * self.price_per_hour


class CostTracker:
    """Thread-safe fleet cost tracker with budget ceiling enforcement.

    Usage::

        tracker = CostTracker(budget_ceiling=15.0)
        tracker.register_vm("vm-abc", price_per_hour=3.0)
        ...
        if tracker.can_afford_more(proposed_workers=1, remaining_clips=20):
            provision_another_vm()
        ...
        tracker.stop_vm("vm-abc")
    """

    def __init__(
        self,
        budget_ceiling: float = 0.0,
        safety_buffer: float = 0.10,
    ) -> None:
        """
        Args:
            budget_ceiling: Maximum total spend for this production run.
                            0 = no limit (not recommended).
            safety_buffer:  Fraction of budget to keep as buffer (default 10%).
        """
        self._lock = threading.Lock()
        self._budget_ceiling = budget_ceiling
        self._safety_buffer = safety_buffer
        self._vms: dict[str, VMCost] = {}
        self._projected_cost: float = 0.0

    # ------------------------------------------------------------------
    # VM lifecycle
    # ------------------------------------------------------------------

    def register_vm(self, vm_id: str, price_per_hour: float) -> None:
        """Register a new VM and start tracking its cost."""
        with self._lock:
            if vm_id in self._vms:
                logger.warning("CostTracker: VM %s already registered", vm_id)
                return
            self._vms[vm_id] = VMCost(
                vm_id=vm_id,
                price_per_hour=price_per_hour,
                started_at=time.time(),
            )
        logger.info(
            "CostTracker: registered VM %s at $%.2f/hr",
            vm_id, price_per_hour,
        )

    def stop_vm(self, vm_id: str) -> None:
        """Mark a VM as stopped (freezes its cost accumulation)."""
        with self._lock:
            vm = self._vms.get(vm_id)
            if vm and vm.stopped_at == 0.0:
                vm.stopped_at = time.time()
                logger.info(
                    "CostTracker: stopped VM %s — ran %.1f hrs, cost $%.2f",
                    vm_id, vm.hours_running, vm.cost_so_far,
                )

    # ------------------------------------------------------------------
    # Cost queries
    # ------------------------------------------------------------------

    @property
    def actual_cost(self) -> float:
        """Current total spend across all VMs."""
        with self._lock:
            return sum(vm.cost_so_far for vm in self._vms.values())

    @property
    def remaining_budget(self) -> float:
        """Budget remaining (0 if no ceiling set)."""
        if self._budget_ceiling <= 0:
            return float("inf")
        return max(0, self._budget_ceiling - self.actual_cost)

    @property
    def active_vm_count(self) -> int:
        """Number of VMs currently running (not stopped)."""
        with self._lock:
            return sum(1 for vm in self._vms.values() if vm.stopped_at == 0.0)

    def set_projection(self, projected_cost: float) -> None:
        """Set the projected total cost for this production run."""
        self._projected_cost = projected_cost

    def is_over_projection(self, threshold: float = 1.3) -> bool:
        """True if actual cost is >threshold× projected cost."""
        if self._projected_cost <= 0:
            return False
        return self.actual_cost > self._projected_cost * threshold

    # ------------------------------------------------------------------
    # Budget gates
    # ------------------------------------------------------------------

    def can_afford_more(
        self,
        proposed_workers: int,
        remaining_clips: int,
        avg_gen_time_sec: float = 180.0,
    ) -> bool:
        """Gate: can we afford to provision more workers?

        Estimates additional cost conservatively (assumes new workers run
        for the full remaining production time).
        """
        if self._budget_ceiling <= 0:
            return True  # no ceiling — always allow

        with self._lock:
            current_cost = sum(vm.cost_so_far for vm in self._vms.values())
            active_workers = sum(1 for vm in self._vms.values() if vm.stopped_at == 0.0)
            avg_price = (
                sum(vm.price_per_hour for vm in self._vms.values() if vm.stopped_at == 0.0)
                / max(active_workers, 1)
            )

        total_workers = active_workers + proposed_workers
        if total_workers <= 0:
            return False

        # Estimate remaining hours
        remaining_hours = (remaining_clips * avg_gen_time_sec) / (total_workers * 3600)
        # Additional cost = new workers × remaining hours × avg price + boot overhead
        additional_cost = (
            proposed_workers * remaining_hours * avg_price
            + proposed_workers * BOOT_OVERHEAD_HOURS * avg_price
        )

        usable_budget = self._budget_ceiling * (1.0 - self._safety_buffer)
        can_afford = (current_cost + additional_cost) <= usable_budget

        if not can_afford:
            logger.warning(
                "CostTracker: cannot afford %d more workers — "
                "current=$%.2f, additional=$%.2f, budget=$%.2f (usable=$%.2f)",
                proposed_workers, current_cost, additional_cost,
                self._budget_ceiling, usable_budget,
            )

        return can_afford

    def should_stop_provisioning(self) -> bool:
        """True if we've spent enough that no more VMs should be provisioned."""
        if self._budget_ceiling <= 0:
            return False
        usable = self._budget_ceiling * (1.0 - self._safety_buffer)
        return self.actual_cost >= usable

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        """Return a JSON-serialisable cost summary."""
        with self._lock:
            vms = [
                {
                    "vm_id": vm.vm_id,
                    "price_per_hour": vm.price_per_hour,
                    "hours_running": round(vm.hours_running, 2),
                    "cost": round(vm.cost_so_far, 2),
                    "active": vm.stopped_at == 0.0,
                }
                for vm in self._vms.values()
            ]

        return {
            "budget_ceiling": self._budget_ceiling,
            "actual_cost": round(self.actual_cost, 2),
            "remaining_budget": round(self.remaining_budget, 2),
            "projected_cost": round(self._projected_cost, 2),
            "over_projection": self.is_over_projection(),
            "active_vms": self.active_vm_count,
            "vms": vms,
        }
