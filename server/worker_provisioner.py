"""
Worker provisioner — automatic GPU worker lifecycle management.

Solves the chicken-and-egg problem where contract preconditions check
worker health BEFORE the pipeline agent gets a chance to provision VMs.

This module runs as a **pre-pipeline step** in ``_init_pipeline_state``
(pipeline.py) and in the server lifespan.  It:

1. Checks if required workers (TTS, video) are already healthy.
2. If not, provisions Vast.ai GPU VMs via the API.
3. Waits for VMs to reach "running" status.
4. Sets up SSH tunnels from localhost ports to the remote workers.
5. Waits for workers to pass health checks.
6. Updates env vars so contracts and infra_agent see the live URLs.
7. Starts the InfraAgent for continuous monitoring.

Architecture invariants preserved:
- One model per VM — TTS and video on separate VMs.
- Workers must be healthy before any stage that needs them.
- Never silently degrade — if provisioning fails, raise loud.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "").strip().lower() in (
    "1",
    "true",
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class WorkerSpec:
    """Specification for a GPU worker to provision."""

    role: str  # "tts" or "video"
    env_var: str  # e.g. "TTS_WORKER_URL"
    local_port: int  # localhost port for SSH tunnel
    remote_port: int  # port on the GPU VM
    capability: str  # health check key (e.g. "tts", "ltx")
    gpu_type: str = "A100_SXM4"
    min_vram_gb: int = 48
    max_price: float = 2.00
    worker_mode: str = "tts"  # gpu_worker.py --mode argument
    vm_id: str = ""  # populated after provisioning
    ssh_host: str = ""
    ssh_port: int = 0
    tunnel_proc: Optional[subprocess.Popen] = field(default=None, repr=False)


# Default worker specs — TTS and video on separate VMs
TTS_SPEC = WorkerSpec(
    role="tts",
    env_var="TTS_WORKER_URL",
    local_port=8880,
    remote_port=8880,
    capability="tts",
    gpu_type="A100_SXM4",
    min_vram_gb=24,
    max_price=1.50,
    worker_mode="tts",
)

VIDEO_SPEC = WorkerSpec(
    role="video",
    env_var="GPU_WORKER_URL",
    local_port=8881,
    remote_port=8880,
    capability="ltx",
    gpu_type="A100_SXM4",
    min_vram_gb=48,
    max_price=2.50,
    worker_mode="ltx",
)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def check_worker_health(url: str, capability: str, timeout: int = 10) -> bool:
    """Check if a worker at the given URL is healthy and has the capability loaded.

    Returns True if healthy, False otherwise.
    """
    health_url = f"{url.rstrip('/')}/health"
    try:
        req = Request(health_url)
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status") != "ok":
            return False
        loaded_key = f"{capability}_loaded"
        return bool(data.get(loaded_key, False))
    except (URLError, OSError, json.JSONDecodeError, TimeoutError, Exception):
        return False


def check_worker_reachable(url: str, timeout: int = 5) -> bool:
    """Check if a worker URL is reachable (responds to /health, any status)."""
    health_url = f"{url.rstrip('/')}/health"
    try:
        req = Request(health_url)
        with urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Vast.ai provisioning
# ---------------------------------------------------------------------------


def _vast_cmd(args: list[str]) -> dict | list | str:
    """Run a vastai CLI command and return parsed output."""
    api_key = os.environ.get("VAST_API_KEY", "")
    if not api_key:
        raise RuntimeError("VAST_API_KEY not set — cannot provision GPU workers")

    cmd = ["vastai", "--api-key", api_key] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(
                f"vastai command failed (rc={result.returncode}): "
                f"{result.stderr[:500]}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip()
    except FileNotFoundError:
        raise RuntimeError("vastai CLI not installed")
    except subprocess.TimeoutExpired:
        raise RuntimeError("vastai command timed out")


def provision_vm(spec: WorkerSpec) -> str:
    """Provision a Vast.ai GPU VM for the given worker spec.

    Returns the instance ID.
    """
    logger.info(
        "Provisioning %s worker: gpu=%s, vram>=%dGB, max $%.2f/hr",
        spec.role, spec.gpu_type, spec.min_vram_gb, spec.max_price,
    )

    # Search for offers
    # Use query-string filter format for vastai CLI
    query = (
        f"gpu_name={spec.gpu_type} "
        f"gpu_ram>={spec.min_vram_gb * 1024} "  # vast uses MB
        f"dph_total<={spec.max_price} "
        f"rentable=true "
        f"disk_space>=200"
    )

    search_result = _vast_cmd([
        "search", "offers",
        "--type", "on-demand",
        "--order", "dph_total",
        "--raw",
        query,
    ])

    offers = search_result if isinstance(search_result, list) else []

    # If no offers for exact GPU type, broaden search
    if not offers:
        logger.warning(
            "No %s offers found, broadening search to any GPU with >=%dGB VRAM",
            spec.gpu_type, spec.min_vram_gb,
        )
        query = (
            f"gpu_ram>={spec.min_vram_gb * 1024} "
            f"dph_total<={spec.max_price} "
            f"rentable=true "
            f"disk_space>=200"
        )
        search_result = _vast_cmd([
            "search", "offers",
            "--type", "on-demand",
            "--order", "dph_total",
            "--raw",
            query,
        ])
        offers = search_result if isinstance(search_result, list) else []

    if not offers:
        raise RuntimeError(
            f"No GPU offers found for {spec.role} worker "
            f"(min {spec.min_vram_gb}GB VRAM, max ${spec.max_price}/hr). "
            f"Try increasing max_price or lowering min_vram_gb."
        )

    # Sort by price and pick cheapest
    sorted_offers = sorted(
        offers, key=lambda o: float(o.get("dph_total", 999))
    )
    best = sorted_offers[0]
    offer_id = best.get("id")

    logger.info(
        "Selected offer %s: %s %dx, %.1fGB VRAM, $%.3f/hr",
        offer_id,
        best.get("gpu_name", "unknown"),
        best.get("num_gpus", 1),
        float(best.get("gpu_ram", 0)) / 1024,
        float(best.get("dph_total", 0)),
    )

    # Create instance with bootstrap onstart
    b2_key_id = os.environ.get("B2_KEY_ID", "")
    b2_app_key = os.environ.get("B2_APPLICATION_KEY", "")
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "")

    onstart = (
        "apt-get update && apt-get install -y git curl && "
        "git clone https://github.com/OrpingtonClose/economy-documentary.git "
        "/workspace/economy-documentary 2>/dev/null || "
        "(cd /workspace/economy-documentary && git pull origin main) && "
        f"B2_KEY_ID={shlex.quote(b2_key_id)} B2_APPLICATION_KEY={shlex.quote(b2_app_key)} "
        "bash /workspace/economy-documentary/scripts/gpu_bootstrap.sh && "
        f"DASHSCOPE_API_KEY={shlex.quote(dashscope_key)} "
        "python3 /workspace/economy-documentary/scripts/gpu_worker.py "
        f"--mode {shlex.quote(spec.worker_mode)} --port {spec.remote_port}"
    )

    create_result = _vast_cmd([
        "create", "instance",
        str(offer_id),
        "--image", "pytorch/pytorch:2.3.0-cuda12.1-cudnn8-devel",
        "--disk", "224",
        "--ssh",
        "--direct",
        "--onstart-cmd", onstart,
        "--raw",
    ])

    if isinstance(create_result, dict):
        instance_id = create_result.get("new_contract")
        if instance_id:
            spec.vm_id = str(instance_id)
            logger.info("VM provisioned: instance_id=%s", spec.vm_id)
            return spec.vm_id

    raise RuntimeError(
        f"Failed to provision {spec.role} VM: unexpected response: {create_result}"
    )


def wait_for_vm_running(spec: WorkerSpec, timeout: int = 600) -> dict:
    """Wait for a provisioned VM to reach 'running' status.

    Returns the VM info dict with SSH connection details.
    """
    logger.info(
        "Waiting for %s VM %s to start (timeout %ds)...",
        spec.role, spec.vm_id, timeout,
    )
    start = time.time()
    while time.time() - start < timeout:
        try:
            result = _vast_cmd(["show", "instance", spec.vm_id, "--raw"])
            if isinstance(result, dict):
                status = result.get(
                    "actual_status", result.get("status_msg", "unknown")
                )
                elapsed = int(time.time() - start)
                logger.info(
                    "  %s VM %s: status=%s (%ds)",
                    spec.role, spec.vm_id, status, elapsed,
                )
                if status == "running":
                    spec.ssh_host = result.get("ssh_host", "")
                    spec.ssh_port = int(result.get("ssh_port", 0))
                    return result
        except Exception as exc:
            logger.warning("  Error checking VM status: %s", exc)

        time.sleep(15)

    raise RuntimeError(
        f"{spec.role} VM {spec.vm_id} did not reach 'running' "
        f"within {timeout}s"
    )


# ---------------------------------------------------------------------------
# SSH tunnel management
# ---------------------------------------------------------------------------


def setup_ssh_tunnel(spec: WorkerSpec) -> subprocess.Popen:
    """Set up an SSH tunnel from localhost:local_port to the GPU VM.

    Returns the tunnel subprocess.
    """
    if not spec.ssh_host or not spec.ssh_port:
        raise RuntimeError(
            f"Cannot set up SSH tunnel for {spec.role}: "
            f"no SSH connection details (host={spec.ssh_host}, port={spec.ssh_port})"
        )

    tunnel_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-N",  # no remote command
        "-L", f"{spec.local_port}:localhost:{spec.remote_port}",
        "-p", str(spec.ssh_port),
        f"root@{spec.ssh_host}",
    ]

    logger.info(
        "Setting up SSH tunnel: localhost:%d -> %s:%d (via %s:%d)",
        spec.local_port, spec.ssh_host, spec.remote_port,
        spec.ssh_host, spec.ssh_port,
    )

    proc = subprocess.Popen(
        tunnel_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Give the tunnel a moment to establish
    time.sleep(3)

    if proc.poll() is not None:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(
            f"SSH tunnel for {spec.role} failed immediately: {stderr}"
        )

    spec.tunnel_proc = proc
    logger.info(
        "SSH tunnel established: localhost:%d -> %s VM %s",
        spec.local_port, spec.role, spec.vm_id,
    )
    return proc


# ---------------------------------------------------------------------------
# Wait for worker health
# ---------------------------------------------------------------------------


def wait_for_worker_healthy(
    spec: WorkerSpec,
    timeout: int = 900,
    poll_interval: int = 15,
) -> bool:
    """Wait for a worker to become healthy after provisioning.

    The worker needs time to:
    1. Boot the VM
    2. Run gpu_bootstrap.sh (install deps, download models)
    3. Start gpu_worker.py
    4. Load the model into VRAM

    This can take 10-15 minutes for a fresh VM.
    """
    url = f"http://localhost:{spec.local_port}"
    logger.info(
        "Waiting for %s worker at %s to become healthy (timeout %ds)...",
        spec.role, url, timeout,
    )

    start = time.time()
    last_status = "unknown"
    while time.time() - start < timeout:
        elapsed = int(time.time() - start)

        # Check if tunnel is still alive
        if spec.tunnel_proc and spec.tunnel_proc.poll() is not None:
            logger.warning(
                "SSH tunnel for %s died — restarting...", spec.role
            )
            try:
                setup_ssh_tunnel(spec)
            except Exception as exc:
                logger.error("Failed to restart tunnel: %s", exc)

        # Check health
        reachable = check_worker_reachable(url, timeout=5)
        if reachable:
            healthy = check_worker_health(url, spec.capability, timeout=10)
            if healthy:
                logger.info(
                    "%s worker at %s is HEALTHY after %ds",
                    spec.role, url, elapsed,
                )
                return True
            if last_status != "reachable_not_loaded":
                logger.info(
                    "  %s worker reachable but model not loaded yet (%ds)",
                    spec.role, elapsed,
                )
                last_status = "reachable_not_loaded"
        else:
            if last_status != "unreachable":
                logger.info(
                    "  %s worker not yet reachable (%ds) — "
                    "VM still bootstrapping...",
                    spec.role, elapsed,
                )
                last_status = "unreachable"

        time.sleep(poll_interval)

    logger.error(
        "%s worker at %s did not become healthy within %ds",
        spec.role, url, timeout,
    )
    return False


# ---------------------------------------------------------------------------
# High-level orchestrator
# ---------------------------------------------------------------------------


class WorkerProvisioner:
    """Manages the full lifecycle of GPU workers for the pipeline.

    Call ``ensure_workers_ready()`` before the pipeline starts.
    Call ``cleanup()`` when the pipeline finishes.
    """

    def __init__(self) -> None:
        self._specs: list[WorkerSpec] = []
        self._lock = threading.Lock()
        self._provisioned = False

    def ensure_workers_ready(
        self,
        require_tts: bool = True,
        require_video: bool = True,
        provision_timeout: int = 900,
    ) -> dict:
        """Ensure all required workers are healthy, provisioning if needed.

        This is the main entry point called by the pipeline before stages
        that need GPU workers.

        Returns a status dict with worker details.
        """
        if _TEST_MODE:
            logger.info(
                "WorkerProvisioner: TEST MODE — skipping worker provisioning"
            )
            return {"status": "test_mode", "workers": []}

        status = {"workers": [], "provisioned": [], "already_healthy": []}

        # Check which workers we need
        specs_needed: list[WorkerSpec] = []
        if require_tts:
            specs_needed.append(WorkerSpec(
                role="tts",
                env_var="TTS_WORKER_URL",
                local_port=TTS_SPEC.local_port,
                remote_port=TTS_SPEC.remote_port,
                capability="tts",
                gpu_type=TTS_SPEC.gpu_type,
                min_vram_gb=TTS_SPEC.min_vram_gb,
                max_price=TTS_SPEC.max_price,
                worker_mode="tts",
            ))
        if require_video:
            specs_needed.append(WorkerSpec(
                role="video",
                env_var="GPU_WORKER_URL",
                local_port=VIDEO_SPEC.local_port,
                remote_port=VIDEO_SPEC.remote_port,
                capability="ltx",
                gpu_type=VIDEO_SPEC.gpu_type,
                min_vram_gb=VIDEO_SPEC.min_vram_gb,
                max_price=VIDEO_SPEC.max_price,
                worker_mode="ltx",
            ))

        for spec in specs_needed:
            url = os.environ.get(spec.env_var, "")
            if not url:
                url = f"http://localhost:{spec.local_port}"

            # Check if already healthy
            if check_worker_health(url, spec.capability):
                logger.info(
                    "%s worker at %s is already healthy — no provisioning needed",
                    spec.role, url,
                )
                status["already_healthy"].append(spec.role)
                status["workers"].append({
                    "role": spec.role,
                    "url": url,
                    "status": "healthy",
                    "provisioned": False,
                })
                continue

            # Need to provision
            logger.info(
                "%s worker at %s is NOT healthy — provisioning new VM...",
                spec.role, url,
            )
            try:
                self._provision_and_connect(spec, provision_timeout)
                status["provisioned"].append(spec.role)
                status["workers"].append({
                    "role": spec.role,
                    "url": f"http://localhost:{spec.local_port}",
                    "status": "healthy",
                    "provisioned": True,
                    "vm_id": spec.vm_id,
                })

                # Update env var so contracts see the new URL
                new_url = f"http://localhost:{spec.local_port}"
                os.environ[spec.env_var] = new_url
                logger.info(
                    "Updated %s=%s", spec.env_var, new_url,
                )

            except Exception as exc:
                logger.error(
                    "Failed to provision %s worker: %s", spec.role, exc,
                )
                # Kill SSH tunnels from any previously-provisioned specs
                # to avoid orphaned subprocess leaks.
                for prev_spec in specs_needed:
                    if prev_spec.tunnel_proc and prev_spec.tunnel_proc.poll() is None:
                        logger.info(
                            "Cleaning up SSH tunnel for %s (pid=%d) after failure",
                            prev_spec.role, prev_spec.tunnel_proc.pid,
                        )
                        prev_spec.tunnel_proc.terminate()
                        try:
                            prev_spec.tunnel_proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            prev_spec.tunnel_proc.kill()
                status["workers"].append({
                    "role": spec.role,
                    "url": url,
                    "status": "failed",
                    "error": str(exc),
                })
                raise RuntimeError(
                    f"Cannot start pipeline: {spec.role} worker provisioning "
                    f"failed: {exc}"
                ) from exc

        with self._lock:
            self._specs = specs_needed
            self._provisioned = True

        # Start InfraAgent for continuous monitoring
        self._start_infra_agent()

        status["status"] = "ready"
        logger.info(
            "WorkerProvisioner: all workers ready. "
            "Already healthy: %s. Provisioned: %s.",
            status["already_healthy"], status["provisioned"],
        )
        return status

    def _provision_and_connect(
        self, spec: WorkerSpec, timeout: int = 900
    ) -> None:
        """Provision a VM, set up tunnel, and wait for health.

        Full lifecycle for a single worker.  The wall-clock ``timeout``
        is tracked across all steps so sub-steps never overshoot.
        """
        _start = time.time()

        # Step 1: Provision VM
        provision_vm(spec)

        # Step 2: Wait for VM to be running
        elapsed = int(time.time() - _start)
        vm_timeout = max(min(timeout - elapsed, 600), 60)
        wait_for_vm_running(spec, timeout=vm_timeout)

        # Step 3: Set up SSH tunnel
        setup_ssh_tunnel(spec)

        # Step 4: Wait for worker to be healthy
        # Bootstrap + model download can take 10-15 min
        elapsed = int(time.time() - _start)
        remaining = max(timeout - elapsed, 300)  # at least 5 min for health wait
        healthy = wait_for_worker_healthy(spec, timeout=remaining)
        if not healthy:
            raise RuntimeError(
                f"{spec.role} worker on VM {spec.vm_id} did not become "
                f"healthy within {remaining}s after provisioning"
            )

    def _start_infra_agent(self) -> None:
        """Start the InfraAgent for continuous worker monitoring."""
        try:
            from infra_agent import start_infra_agent

            infra = start_infra_agent(
                poll_interval=30.0, max_consecutive_failures=3
            )
            infra.start()
            logger.info("InfraAgent started for continuous monitoring")
        except Exception as exc:
            logger.warning("Failed to start InfraAgent: %s", exc)

    def cleanup(self) -> None:
        """Clean up: kill SSH tunnels and optionally terminate VMs."""
        with self._lock:
            specs = list(self._specs)

        for spec in specs:
            # Kill SSH tunnel
            if spec.tunnel_proc and spec.tunnel_proc.poll() is None:
                logger.info(
                    "Killing SSH tunnel for %s worker (pid=%d)",
                    spec.role, spec.tunnel_proc.pid,
                )
                spec.tunnel_proc.terminate()
                try:
                    spec.tunnel_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    spec.tunnel_proc.kill()

        # Shutdown InfraAgent
        try:
            from infra_agent import get_infra_agent

            agent = get_infra_agent()
            if agent:
                agent.shutdown()
                logger.info("InfraAgent stopped")
        except Exception:
            pass

    def get_vm_ids(self) -> list[str]:
        """Return the IDs of all provisioned VMs."""
        with self._lock:
            return [s.vm_id for s in self._specs if s.vm_id]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_provisioner: Optional[WorkerProvisioner] = None
_provisioner_lock = threading.Lock()


def get_provisioner() -> WorkerProvisioner:
    """Return the global WorkerProvisioner singleton."""
    global _provisioner
    with _provisioner_lock:
        if _provisioner is None:
            _provisioner = WorkerProvisioner()
    return _provisioner
