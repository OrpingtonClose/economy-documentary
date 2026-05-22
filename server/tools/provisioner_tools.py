"""Agent-callable tools for GPU worker provisioning.

These tools give the escalation agent direct control over the
provisioning process.  When the background provisioner fails, the
agent can read the trace, reason about what happened, and call these
tools to try different strategies — different GPU types, higher price
ceilings, broader reliability filters, specific offers from the
catalog.

The agent has full power within the unit.  It is not limited to
suggesting "retry with broader constraints" — it directly calls
search_offers, create_instance, and check_vm_status with whatever
parameters it decides.
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)


def search_offers(
    min_vram_gb: int = 48,
    max_price: float = 2.00,
    min_disk_gb: int = 50,
    gpu_type: str = "",
    reliability_floor: float = 0.95,
    inet_down_floor: int = 200,
    rentable_only: bool = True,
    limit: int = 20,
) -> str:
    """Search Vast.ai for available GPU offers matching the given constraints.

    Returns a JSON string with the full offer catalog — every viable
    offer with GPU name, VRAM, price, reliability, bandwidth, location.
    The agent reads this to decide which offer to try next.

    Args:
        min_vram_gb: Minimum VRAM in GB. Hard floor — never compromised.
        max_price: Maximum price per hour in USD.
        min_disk_gb: Minimum disk space in GB.
        gpu_type: Exact GPU name (e.g. "A100_SXM4"). Empty = any GPU.
        reliability_floor: Minimum reliability score (0.0-1.0).
        inet_down_floor: Minimum download speed in Mbps.
        rentable_only: Only show rentable offers.
        limit: Max offers to return (for tractability).
    """
    from worker_provisioner import _vast_cmd, _trace, get_provisioner

    vram_mb = min_vram_gb * 1024

    # Build query string
    parts = []
    if gpu_type:
        parts.append(f"gpu_name={gpu_type}")
    parts.append(f"gpu_ram>={min_vram_gb}")
    parts.append(f"dph_total<={max_price}")
    if rentable_only:
        parts.append("rentable=true")
    parts.append(f"reliability>{reliability_floor:.2f}")
    parts.append(f"inet_down>{inet_down_floor}")
    parts.append(f"disk_space>={min_disk_gb}")
    query = " ".join(parts)

    search_result = _vast_cmd([
        "search", "offers",
        "--type", "on-demand",
        "--order", "inet_down-",
        "--raw",
        query,
    ])

    offers = search_result if isinstance(search_result, list) else []

    # Python-side post-filter (CLI filters can be unreliable)
    filtered = []
    for o in offers:
        o_vram = float(o.get("gpu_ram", 0))
        o_price = float(o.get("dph_total", 999))
        o_disk = float(o.get("disk_space", 0))
        o_rel = float(o.get("reliability", 0))
        o_inet = float(o.get("inet_down", 0))
        if (
            o_vram >= vram_mb
            and o_price <= max_price
            and o_disk >= min_disk_gb
            and o_rel >= reliability_floor
            and o_inet >= inet_down_floor
        ):
            filtered.append(o)

    # Sort by download speed then price
    sorted_offers = sorted(
        filtered,
        key=lambda o: (-float(o.get("inet_down", 0)), float(o.get("dph_total", 999))),
    )

    # Format offers for the agent
    catalog = []
    for o in sorted_offers[:limit]:
        catalog.append({
            "id": int(o.get("id", 0)),
            "gpu_name": o.get("gpu_name", "unknown"),
            "gpu_ram_gb": round(float(o.get("gpu_ram", 0)) / 1024, 1),
            "num_gpus": o.get("num_gpus", 1),
            "dph_total": round(float(o.get("dph_total", 0)), 4),
            "disk_space_gb": round(float(o.get("disk_space", 0)), 0),
            "inet_down": round(float(o.get("inet_down", 0)), 0),
            "inet_up": round(float(o.get("inet_up", 0)), 0),
            "reliability": round(float(o.get("reliability", 0)), 3),
            "rentable": o.get("rentable", False),
            "verified": o.get("verified", False),
            "country": o.get("country", ""),
            "host_id": o.get("host_id", ""),
        })

    result = {
        "query": query,
        "raw_results": len(offers) if isinstance(offers, list) else 0,
        "post_filter_results": len(filtered),
        "offers_returned": len(catalog),
        "offers": catalog,
    }

    # Trace the search
    try:
        _prov = get_provisioner()
        if _prov and hasattr(_prov, "tts_spec"):
            _trace(_prov.tts_spec, "agent_search_offers", {
                "query": query,
                "results": len(catalog),
                "agent_initiated": True,
            })
    except Exception:
        pass

    return json.dumps(result)


def create_instance(
    offer_id: int,
    role: str = "tts",
    disk_gb: int = 64,
    worker_mode: str = "tts",
) -> str:
    """Create a Vast.ai instance from a specific offer.

    The agent calls this after reviewing the offer catalog from
    search_offers and deciding which offer to try.  Returns the
    instance ID on success, or an error on failure.

    Args:
        offer_id: The offer ID from search_offers results.
        role: Worker role — "tts" or "video".
        disk_gb: Disk size in GB for the instance.
        worker_mode: Worker mode — "tts", "ltx", or "both".
    """
    from worker_provisioner import (
        _vast_cmd,
        _trace,
        _HEALTH_CONTROL_PORT,
        WorkerSpec,
        normalize_worker_mode,
        resolve_docker_image,
        get_provisioner,
    )
    import shlex
    import subprocess
    import uuid

    worker_mode = normalize_worker_mode(worker_mode)
    _docker_image, _torch_index = resolve_docker_image(worker_mode)

    # Get the spec for this role
    remote_port = 8880 if role == "tts" else 8881
    spec = None
    try:
        _prov = get_provisioner()
        if role == "tts" and hasattr(_prov, "tts_spec"):
            spec = _prov.tts_spec
        elif role == "video" and hasattr(_prov, "video_spec"):
            spec = _prov.video_spec
    except Exception:
        pass

    # Build onstart script
    b2_key_id = os.environ.get("B2_KEY_ID", "")
    b2_app_key = os.environ.get("B2_APPLICATION_KEY", "")
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")

    try:
        _branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            text=True,
        ).strip()
        if not _branch or _branch == "HEAD":
            _branch = "main"
    except Exception:
        _branch = "main"

    _min_torch = "2.7.0"
    for _key, _mspec in _MODEL_MANIFEST.items():
        if _mspec.get("worker_mode") == worker_mode:
            _min_torch = _mspec.get("min_torch", "2.7.0")
            break

    onstart = (
        f"export B2_KEY_ID={shlex.quote(b2_key_id)} && "
        f"export B2_APPLICATION_KEY={shlex.quote(b2_app_key)} && "
        f"export WORKER_MODE={shlex.quote(worker_mode)} && "
        f"export DASHSCOPE_API_KEY={shlex.quote(dashscope_key)} && "
        f"export OPENROUTER_API_KEY={shlex.quote(openrouter_key)} && "
        f"export TORCH_INDEX={shlex.quote(_torch_index)} && "
        f"export MIN_TORCH_VERSION={shlex.quote(_min_torch)} && "
        "export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True && "
        "apt-get update && apt-get install -y git curl ffmpeg libsndfile1 sox libsox-dev && "
        f"(git clone -b {shlex.quote(_branch)} --single-branch "
        "https://github.com/OrpingtonClose/economy-documentary.git "
        "/workspace/economy-documentary 2>&1 || "
        "git clone -b main --single-branch "
        "https://github.com/OrpingtonClose/economy-documentary.git "
        "/workspace/economy-documentary 2>&1 || "
        f"(cd /workspace/economy-documentary && git fetch origin {shlex.quote(_branch)} && "
        f"git checkout {shlex.quote(_branch)} && git pull origin {shlex.quote(_branch)})) && "
        "python3 -c 'import torch; print(f\"torch {torch.__version__} from {torch.__file__}\")' && "
        "pip install --break-system-packages --no-cache-dir "
        "'fastapi>=0.100.0' 'uvicorn>=0.20.0' 'pydantic>=2.0.0' "
        "'numpy>=1.26.0,<2.0.0' 'soundfile>=0.12.0' && "
        "cd /workspace/economy-documentary/server && "
        f"python3 gpu_worker.py --mode {shlex.quote(worker_mode)} --port {remote_port}"
    )

    _run_id = os.environ.get("DOCUMENTARY_RUN_ID", uuid.uuid4().hex[:8])
    _label = f"documentary-{role}-{_run_id}"

    _env_ports = (
        f"-p {remote_port}:{remote_port} "
        f"-p {_HEALTH_CONTROL_PORT}:{_HEALTH_CONTROL_PORT}"
    )

    if spec:
        _trace(spec, "agent_create_instance_attempt", {
            "offer_id": offer_id,
            "docker_image": _docker_image,
            "disk_gb": disk_gb,
            "worker_mode": worker_mode,
            "agent_initiated": True,
        })

    try:
        create_result = _vast_cmd([
            "create", "instance",
            str(offer_id),
            "--image", _docker_image,
            "--disk", str(disk_gb),
            "--ssh",
            "--direct",
            "--env", _env_ports,
            "--label", _label,
            "--onstart-cmd", onstart,
        ])
    except RuntimeError as e:
        if spec:
            _trace(spec, "agent_create_instance_failed", {
                "offer_id": offer_id,
                "error": str(e),
                "error_type": (
                    "no_such_ask" if "no_such_ask" in str(e).lower()
                    else "not_available" if "not available" in str(e).lower()
                    else "other"
                ),
                "agent_initiated": True,
            })
        return json.dumps({
            "status": "error",
            "offer_id": offer_id,
            "error": str(e),
            "error_type": (
                "no_such_ask" if "no_such_ask" in str(e).lower()
                else "not_available" if "not available" in str(e).lower()
                else "other"
            ),
        })

    # Parse response
    vm_id = None
    if isinstance(create_result, dict):
        vm_id = create_result.get("new_contract")
    elif isinstance(create_result, str) and "new_contract" in create_result:
        import re
        match = re.search(r"'new_contract'\s*:\s*(\d+)", create_result)
        if match:
            vm_id = match.group(1)

    if vm_id:
        try:
            from tools.vastai_tools import register_owned_vm
            register_owned_vm(str(vm_id))
        except Exception:
            pass
        if spec:
            spec.vm_id = str(vm_id)
            _trace(spec, "agent_create_instance_success", {
                "offer_id": offer_id,
                "vm_id": str(vm_id),
                "agent_initiated": True,
            })
        return json.dumps({
            "status": "created",
            "offer_id": offer_id,
            "vm_id": str(vm_id),
        })

    return json.dumps({
        "status": "error",
        "offer_id": offer_id,
        "error": f"Unexpected response: {create_result}",
    })


def check_vm_status(vm_id: str) -> str:
    """Check the status of a Vast.ai VM instance.

    Returns VM status, connection details, and health endpoint
    response if available.

    Args:
        vm_id: The instance ID returned by create_instance.
    """
    from worker_provisioner import _vast_cmd

    try:
        result = _vast_cmd(["show", "instance", vm_id, "--raw"])
    except RuntimeError as e:
        return json.dumps({"status": "error", "vm_id": vm_id, "error": str(e)})

    if not isinstance(result, dict):
        return json.dumps({
            "status": "error",
            "vm_id": vm_id,
            "raw": str(result)[:500],
        })

    actual_status = result.get("actual_status", "unknown")
    public_ipaddr = result.get("public_ipaddr", "")
    direct_port = result.get("direct_port", 0)
    ssh_host = result.get("ssh_host", "")
    ssh_port = result.get("ssh_port", 0)

    # Try worker endpoint if running
    health_text = None
    if actual_status == "running" and public_ipaddr and direct_port:
        try:
            from urllib.request import Request, urlopen
            health_url = f"http://{public_ipaddr}:{direct_port}/"
            req = Request(health_url)
            with urlopen(req, timeout=10) as resp:
                health_text = resp.read().decode().strip()
        except Exception as e:
            health_text = f"error: {e}"

    return json.dumps({
        "status": actual_status,
        "vm_id": vm_id,
        "public_ipaddr": public_ipaddr,
        "direct_port": direct_port,
        "ssh_host": ssh_host,
        "ssh_port": ssh_port,
        "health_text": health_text,
    })


def get_provision_trace(role: str = "tts") -> str:
    """Get the full provision trace for a worker role.

    Returns every trace entry — every search query, every offer
    considered, every constraint applied, every create attempt, every
    failure.  This is the agent's complete observation log for
    reasoning about what happened and what to try next.

    Args:
        role: "tts" or "video".
    """
    from worker_provisioner import get_provisioner

    try:
        _prov = get_provisioner()
        if role == "tts" and hasattr(_prov, "tts_spec"):
            trace = _prov.tts_spec.provision_trace
        elif role == "video" and hasattr(_prov, "video_spec"):
            trace = _prov.video_spec.provision_trace
        else:
            trace = []
    except Exception as e:
        return json.dumps({"error": str(e), "trace": []})

    return json.dumps({
        "role": role,
        "entries": len(trace),
        "trace": trace,
    })

