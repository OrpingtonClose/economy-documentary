"""
Vast.ai API tools -- GPU VM provisioning, status, and termination.

All credentials via VAST_API_KEY env var.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Optional

from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

_VAST_API_KEY = os.environ.get("VAST_API_KEY", "")
_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "").strip().lower() in ("1", "true")


def _vast_cmd(args: list[str]) -> dict:
    """Run a vastai CLI command and return parsed output."""
    if not _VAST_API_KEY:
        return {"error": "VAST_API_KEY not set"}

    cmd = ["vastai", "--api-key", _VAST_API_KEY] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return {
                "error": f"vastai command failed (rc={result.returncode})",
                "stderr": result.stderr[:500],
            }
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"output": result.stdout.strip()}
    except FileNotFoundError:
        return {"error": "vastai CLI not installed"}
    except subprocess.TimeoutExpired:
        return {"error": "vastai command timed out"}


def provision_gpu_vm(
    gpu_type: str = "RTX_4090",
    min_vram_gb: int = 24,
    max_price: float = 0.50,
    tool_context=None,
) -> str:
    """Provision a GPU VM via Vast.ai API.

    Args:
        gpu_type: GPU type to request (e.g., "RTX_4090", "A100").
        min_vram_gb: Minimum VRAM in GB.
        max_price: Maximum price per hour in USD.

    Returns:
        JSON string with VM details or error.
    """
    if _TEST_MODE:
        return json.dumps(
            {
                "status": "test_mode",
                "vm_id": "test-vm-001",
                "message": "Test mode: no actual VM provisioned",
                "gpu_type": gpu_type,
                "min_vram_gb": min_vram_gb,
                "max_price": max_price,
            }
        )

    # Search for available instances
    search_result = _vast_cmd(
        [
            "search", "offers",
            "--type", "on-demand",
            "--gpu-name", gpu_type,
            "--min-gpu-ram", str(min_vram_gb),
            "--max-dph", str(max_price),
            "--raw",
        ]
    )

    if "error" in search_result:
        return json.dumps(search_result)

    # Parse offers and select best match (lowest price with sufficient VRAM)
    offers = search_result if isinstance(search_result, list) else []
    if not offers:
        return json.dumps(
            {"status": "no_offers", "error": "No matching GPU offers found"}
        )

    # Sort by dph_total (price per hour) ascending
    sorted_offers = sorted(
        offers,
        key=lambda o: float(o.get("dph_total", 999)),
    )

    best = sorted_offers[0]
    offer_id = best.get("id")
    if not offer_id:
        return json.dumps(
            {"status": "error", "error": "Best offer has no ID", "offer": best}
        )

    logger.info(
        "Selected offer %s: %s, %.1fGB VRAM, $%.3f/hr",
        offer_id,
        best.get("gpu_name", "unknown"),
        float(best.get("gpu_ram", 0)) / 1024,
        float(best.get("dph_total", 0)),
    )

    # Create instance from best offer
    # Use pytorch template for CUDA + PyTorch pre-installed
    create_result = _vast_cmd(
        [
            "create", "instance",
            str(offer_id),
            "--image", "pytorch/pytorch:2.3.0-cuda12.1-cudnn8-devel",
            "--disk", "80",
            "--raw",
        ]
    )

    if "error" in create_result:
        return json.dumps(create_result)

    instance_id = create_result.get("new_contract")
    return json.dumps(
        {
            "status": "provisioned",
            "vm_id": str(instance_id),
            "offer_id": str(offer_id),
            "gpu_name": best.get("gpu_name", "unknown"),
            "gpu_ram_gb": round(float(best.get("gpu_ram", 0)) / 1024, 1),
            "price_per_hour": round(float(best.get("dph_total", 0)), 3),
            "message": "VM provisioned. Wait for status=running then bootstrap.",
        }
    )


def check_vm_status(vm_id: str, tool_context=None) -> str:
    """Check if a Vast.ai VM is ready.

    Args:
        vm_id: The VM instance ID.

    Returns:
        JSON string with VM status.
    """
    if _TEST_MODE:
        return json.dumps(
            {
                "vm_id": vm_id,
                "status": "running",
                "mode": "test",
            }
        )

    result = _vast_cmd(["show", "instance", vm_id, "--raw"])
    return json.dumps(result)


def terminate_vm(vm_id: str, tool_context=None) -> str:
    """Stop and destroy a Vast.ai VM.

    Args:
        vm_id: The VM instance ID to terminate.

    Returns:
        JSON string with termination result.
    """
    if _TEST_MODE:
        return json.dumps(
            {
                "vm_id": vm_id,
                "status": "terminated",
                "mode": "test",
            }
        )

    result = _vast_cmd(["destroy", "instance", vm_id])
    return json.dumps(result)


def list_active_vms(tool_context=None) -> str:
    """List all running Vast.ai VMs.

    Returns:
        JSON string with list of active VMs.
    """
    if _TEST_MODE:
        return json.dumps(
            {
                "vms": [],
                "mode": "test",
                "message": "No VMs in test mode",
            }
        )

    result = _vast_cmd(["show", "instances", "--raw"])
    return json.dumps(result)


# -- ADK FunctionTool wrappers -------------------------------------------------
provision_gpu_vm_tool = FunctionTool(provision_gpu_vm)
check_vm_status_tool = FunctionTool(check_vm_status)
terminate_vm_tool = FunctionTool(terminate_vm)
list_active_vms_tool = FunctionTool(list_active_vms)

vastai_tools = [
    provision_gpu_vm_tool,
    check_vm_status_tool,
    terminate_vm_tool,
    list_active_vms_tool,
]
