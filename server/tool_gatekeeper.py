"""Tool Gatekeeper — ADVISORY state tracker + budget reporter.

The agent REMAINS the decision-maker. This module ONLY:
- Tracks VM lifecycle state and tool-call history
- Reports budgets back to the agent so it can reason
- Provides fleet state for get_vm_budget tool

NO hard blocks. The agent decides whether to proceed.
The agent calls get_fleet_state, reasons, then acts.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _VmLifecycle:
    """Track lifecycle of a single VM for one role."""

    vm_id: str = ""
    role: str = ""
    state: str = "none"  # none → requested → provisioning → running → healthy | error
    status_checks: int = 0
    health_checks: int = 0
    ssh_attempts: int = 0
    provision_attempts: int = 0
    created_at: float = 0.0
    last_status_check: float = 0.0
    last_health_check: float = 0.0
    last_ssh: float = 0.0
    errors: list[str] = field(default_factory=list)


class ToolGatekeeper:
    """Advisory gatekeeper — tracks state, reports budgets, never blocks."""

    # Recommended limits (agent can exceed with reason)
    _REC_STATUS_CHECKS = 15
    _REC_HEALTH_CHECKS = 10
    _REC_SSH_ATTEMPTS = 8
    _REC_PROVISION_ATTEMPTS = 2

    # Recommended intervals (agent can exceed with reason)
    _REC_STATUS_INTERVAL = 20.0
    _REC_HEALTH_INTERVAL = 20.0
    _REC_SSH_INTERVAL = 15.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._vms: dict[str, _VmLifecycle] = {}

    # ------------------------------------------------------------------
    # Recording — called by graph pipeline wrappers after tool execution
    # ------------------------------------------------------------------
    def on_provision(self, role: str, vm_id: str, success: bool) -> None:
        with self._lock:
            vm = self._vms.get(role)
            if vm is None:
                vm = _VmLifecycle(role=role, vm_id=vm_id)
                self._vms[role] = vm
            vm.vm_id = vm_id
            vm.provision_attempts += 1
            vm.state = "provisioning" if success else "error"
            if not success:
                vm.errors.append(f"provision_attempt_{vm.provision_attempts}_failed")

    def on_status(self, role: str, status: str) -> None:
        with self._lock:
            vm = self._vms.get(role)
            if vm is None:
                return
            vm.status_checks += 1
            vm.last_status_check = time.time()
            if status in ("running", "running"):
                if vm.state in ("none", "requested", "provisioning"):
                    vm.state = "running"
            elif status in ("loading",):
                if vm.state == "none":
                    vm.state = "provisioning"

    def on_health(self, role: str, healthy: bool) -> None:
        with self._lock:
            vm = self._vms.get(role)
            if vm is None:
                return
            vm.health_checks += 1
            vm.last_health_check = time.time()
            if healthy:
                vm.state = "healthy"
            elif vm.state == "healthy":
                vm.state = "error"
                vm.errors.append("health_degraded")

    def on_ssh(self, role: str) -> None:
        with self._lock:
            vm = self._vms.get(role)
            if vm is None:
                return
            vm.ssh_attempts += 1
            vm.last_ssh = time.time()

    def on_destroy(self, role: str) -> None:
        with self._lock:
            self._vms.pop(role, None)

    # ------------------------------------------------------------------
    # Advisory — agent calls these to inform its reasoning
    # ------------------------------------------------------------------
    def get_state(self, role: str) -> dict[str, Any]:
        """Return full state + budget for agent reasoning."""
        with self._lock:
            vm = self._vms.get(role)
            if vm is None:
                return {
                    "state": "none",
                    "vm_id": "",
                    "budget": self._empty_budget(),
                    "recommendation": "No VM. Call search_gpu_offers then provision_vm.",
                }
            return {
                "state": vm.state,
                "vm_id": vm.vm_id,
                "budget": self._budget(vm),
                "recommendation": self._recommend(vm),
            }

    def get_fleet_summary(self) -> dict[str, Any]:
        """Summary of all VMs for agent overview."""
        with self._lock:
            return {
                role: {
                    "state": vm.state,
                    "vm_id": vm.vm_id,
                    "provision_attempts": vm.provision_attempts,
                }
                for role, vm in self._vms.items()
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _empty_budget(self) -> dict[str, Any]:
        return {
            "status_checks_used": 0,
            "status_checks_recommended": self._REC_STATUS_CHECKS,
            "health_checks_used": 0,
            "health_checks_recommended": self._REC_HEALTH_CHECKS,
            "ssh_attempts_used": 0,
            "ssh_attempts_recommended": self._REC_SSH_ATTEMPTS,
            "provision_attempts_used": 0,
            "provision_attempts_recommended": self._REC_PROVISION_ATTEMPTS,
        }

    def _budget(self, vm: _VmLifecycle) -> dict[str, Any]:
        now = time.time()
        return {
            "status_checks_used": vm.status_checks,
            "status_checks_recommended": self._REC_STATUS_CHECKS,
            "status_checks_remaining": max(0, self._REC_STATUS_CHECKS - vm.status_checks),
            "health_checks_used": vm.health_checks,
            "health_checks_recommended": self._REC_HEALTH_CHECKS,
            "health_checks_remaining": max(0, self._REC_HEALTH_CHECKS - vm.health_checks),
            "ssh_attempts_used": vm.ssh_attempts,
            "ssh_attempts_recommended": self._REC_SSH_ATTEMPTS,
            "ssh_attempts_remaining": max(0, self._REC_SSH_ATTEMPTS - vm.ssh_attempts),
            "provision_attempts_used": vm.provision_attempts,
            "provision_attempts_recommended": self._REC_PROVISION_ATTEMPTS,
            "provision_attempts_remaining": max(0, self._REC_PROVISION_ATTEMPTS - vm.provision_attempts),
            "seconds_since_last_status": round(now - vm.last_status_check, 1) if vm.last_status_check else None,
            "seconds_since_last_health": round(now - vm.last_health_check, 1) if vm.last_health_check else None,
            "seconds_since_last_ssh": round(now - vm.last_ssh, 1) if vm.last_ssh else None,
            "errors": vm.errors,
        }

    def _recommend(self, vm: _VmLifecycle) -> str:
        if vm.state == "none":
            return "No VM. Call search_gpu_offers then provision_vm."
        if vm.state == "provisioning":
            return f"VM {vm.vm_id} booting. Wait 30s. {vm.status_checks}/{self._REC_STATUS_CHECKS} status checks used."
        if vm.state == "running":
            return f"VM {vm.vm_id} running. Check health every 30s. {vm.health_checks}/{self._REC_HEALTH_CHECKS} checks used."
        if vm.state == "healthy":
            return f"VM {vm.vm_id} healthy. Submit jobs."
        if vm.state == "error":
            return f"VM {vm.vm_id} error. SSH to diagnose ({vm.ssh_attempts}/{self._REC_SSH_ATTEMPTS} used) or destroy."
        return f"VM {vm.vm_id} state: {vm.state}"


# Singleton
gatekeeper = ToolGatekeeper()
