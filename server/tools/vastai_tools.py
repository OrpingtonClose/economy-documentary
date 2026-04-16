"""
Vast.ai API tools -- GPU VM provisioning, status, and termination.

All credentials via VAST_API_KEY env var.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

from strands import tool
from worker_provisioner import resolve_docker_image

# ---------------------------------------------------------------------------
# VM ownership registry (GAP 2.1)
# ---------------------------------------------------------------------------
_OWNED_VMS_FILE = "/tmp/documentary-pipeline/_owned_instances.json"


def _load_owned_vms() -> set[str]:
    """Load the set of VM IDs created by this pipeline run."""
    try:
        with open(_OWNED_VMS_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_owned_vms(vms: set[str]) -> None:
    """Persist the owned-VM set to disk."""
    os.makedirs(os.path.dirname(_OWNED_VMS_FILE), exist_ok=True)
    with open(_OWNED_VMS_FILE, "w") as f:
        json.dump(sorted(vms), f)


def register_owned_vm(vm_id: str) -> None:
    """Register a VM as owned by this pipeline run."""
    vms = _load_owned_vms()
    vms.add(str(vm_id))
    _save_owned_vms(vms)
    logger.info("Registered owned VM: %s (total owned: %d)", vm_id, len(vms))

logger = logging.getLogger(__name__)

_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "").strip().lower() in ("1", "true")


import ast as _ast
import re as _re


def _vast_cmd(args: list[str]) -> dict | list:
    """Run a vastai CLI command and return parsed output.

    The vastai CLI v1.0 has inconsistent output formats:
    - ``search offers --raw`` returns JSON arrays
    - ``show instance --raw`` returns JSON objects
    - ``create instance`` returns Python-repr text like
      ``Started. {'success': True, 'new_contract': 12345, ...}``
      (NOT valid JSON — uses single quotes and Python booleans)
    - ``create instance --raw`` returns **empty** output
    - ``destroy instance`` returns plain text like
      ``destroying instance 12345.``

    We try JSON first, then fall back to Python ``ast.literal_eval``
    to handle the repr-style create output.
    """
    api_key = os.environ.get("VAST_API_KEY", "")
    if not api_key:
        return {"error": "VAST_API_KEY not set"}

    cmd = ["vastai", "--api-key", api_key] + args
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
        stdout = result.stdout.strip()
        if not stdout:
            return {"output": ""}
        # Try JSON first (search/show commands with --raw)
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            pass
        # Try Python repr (create instance without --raw returns e.g.
        # "Started. {'success': True, 'new_contract': 12345, ...}")
        match = _re.search(r"(\{.*\})", stdout, _re.DOTALL)
        if match:
            try:
                return _ast.literal_eval(match.group(1))
            except (ValueError, SyntaxError):
                pass
        return {"output": stdout}
    except FileNotFoundError:
        return {"error": "vastai CLI not installed"}
    except subprocess.TimeoutExpired:
        return {"error": "vastai command timed out"}


@tool
def provision_gpu_vm(
    gpu_type: str = "A100_SXM4",
    min_vram_gb: int = 48,
    max_price: float = 1.50,
    tool_context=None,
) -> str:
    """Provision a GPU VM via Vast.ai API.

    LTX-2.3 with enable_model_cpu_offload() requires 48GB+ VRAM
    (Gemma3 text encoder alone is ~46GB bf16).

    Args:
        gpu_type: GPU type to request (e.g., "A100_SXM4", "L40S",
            "RTX_5090", "H200").  Use "any" to skip GPU name filter.
        min_vram_gb: Minimum VRAM in GB (must be >= 48 for LTX-2.3).
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

    # Fetch ALL on-demand offers from Vast.ai, then filter in Python.
    # The vastai CLI v1.0's query-string filtering is unreliable when
    # called via subprocess (silently returns empty results), so we
    # always fetch the full list and apply filters ourselves.
    search_result = _vast_cmd(
        ["search", "offers", "--type", "on-demand", "--raw"]
    )

    if isinstance(search_result, dict) and "error" in search_result:
        return json.dumps(search_result)

    all_offers = search_result if isinstance(search_result, list) else []
    if not all_offers:
        return json.dumps(
            {"status": "no_offers", "error": "Vast.ai returned no offers at all"}
        )

    # Filter offers in Python — reliable regardless of CLI version.
    # VRAM is in MB in the API (e.g. 81920 = 80GB).
    min_vram_mb = min_vram_gb * 1024
    gpu_display = gpu_type.replace("_", " ").lower() if gpu_type else ""

    offers = []
    for o in all_offers:
        vram = float(o.get("gpu_ram", 0))
        price = float(o.get("dph_total", 999))
        rentable = o.get("rentable", False)
        verified = o.get("verification") not in ("unverified",)
        gpu_name = (o.get("gpu_name", "") or "").lower()

        if vram < min_vram_mb:
            continue
        if price > max_price:
            continue
        if not rentable:
            continue
        if not verified:
            continue
        # GPU type filter (skip if "any")
        if gpu_display and gpu_display != "any" and gpu_display not in gpu_name:
            continue
        offers.append(o)

    logger.info(
        "Vast.ai: %d/%d offers match (gpu_type=%s, min_vram=%dGB, max_price=$%.2f/hr)",
        len(offers), len(all_offers), gpu_type, min_vram_gb, max_price,
    )

    if not offers:
        return json.dumps(
            {
                "status": "no_offers",
                "error": f"No matching GPU offers found (gpu_type={gpu_type}, "
                         f"min_vram_gb={min_vram_gb}, max_price=${max_price}/hr)",
                "total_offers_checked": len(all_offers),
            }
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

    # Create instance from best offer.
    # Docker image is resolved from config/model_manifest.json based on model
    # runtime requirements (min_torch, min_cuda).  Default to "ltx" worker
    # mode since this ADK tool is typically used for video generation VMs.
    # NOTE: Do NOT pass --raw — vastai CLI returns empty stdout with --raw
    # for create commands.  Without --raw it returns Python repr that we
    # parse via ast.literal_eval in _vast_cmd.
    _docker_image, _torch_index = resolve_docker_image("ltx")
    create_result = _vast_cmd(
        [
            "create", "instance",
            str(offer_id),
            "--image", _docker_image,
            "--disk", "224",
            "--ssh",
            "--direct",
            "--env", "-p 8880:8880",
        ]
    )

    if "error" in create_result:
        return json.dumps(create_result)

    instance_id = create_result.get("new_contract")
    if not instance_id:
        return json.dumps(
            {"status": "error", "error": "No instance ID in create response", "response": create_result}
        )

    # GAP 2.1: Register as owned so terminate_vm() accepts it
    register_owned_vm(str(instance_id))

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


@tool
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


@tool
def terminate_vm(vm_id: str, tool_context=None) -> str:
    """Stop and destroy a Vast.ai VM — ONLY if this pipeline created it.

    Args:
        vm_id: The VM instance ID to terminate.

    Returns:
        JSON string with termination result or refusal.
    """
    if _TEST_MODE:
        return json.dumps(
            {
                "vm_id": vm_id,
                "status": "terminated",
                "mode": "test",
            }
        )

    # GAP 2.1: Ownership guard — refuse to destroy VMs we didn't create
    owned = _load_owned_vms()
    if str(vm_id) not in owned:
        error_msg = (
            f"REFUSED: VM {vm_id} was NOT created by this pipeline run. "
            f"Owned VMs: {sorted(owned)}. "
            f"NEVER destroy VMs you didn't create — they may belong to "
            f"other processes on the shared Vast.ai account."
        )
        logger.error(error_msg)
        return json.dumps({"error": error_msg, "status": "refused"})

    result = _vast_cmd(["destroy", "instance", vm_id])
    # Remove from registry only after confirmed destruction
    if isinstance(result, dict) and "error" not in result:
        owned.discard(str(vm_id))
        _save_owned_vms(owned)
    return json.dumps(result)


@tool
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


vastai_tools = [provision_gpu_vm, check_vm_status, terminate_vm, list_active_vms]

vastai_tools = [
    provision_gpu_vm_tool,
    check_vm_status_tool,
    terminate_vm_tool,
    list_active_vms_tool,
]
