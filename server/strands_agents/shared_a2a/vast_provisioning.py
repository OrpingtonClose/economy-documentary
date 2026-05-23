"""Agentic Vast.ai provisioning tools.

These tools expose the Vast.ai CLI to the production agent so it can
search, compare, and provision GPU workers autonomously.
"""
from __future__ import annotations

import json
import os
import subprocess

from strands import tool

logger = __import__("logging").getLogger(__name__)


def _vast_cmd(args: list[str]) -> dict | list | str:
    """Run a vastai CLI command and return parsed JSON output."""
    raw_key = os.environ.get("VAST_AI_KEY", "") or os.environ.get("VAST_API_KEY", "")
    api_key = raw_key.split()[0].strip() if raw_key else ""
    if not api_key:
        raise RuntimeError("VAST_AI_KEY not set")
    cmd = ["vastai", "--api-key", api_key] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"vastai {args[0]} failed: {result.stderr[:500]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()


def _ssh_cmd(instance_id: int, remote_cmd: str) -> str:
    """Run an SSH command on a Vast.ai instance."""
    try:
        info = _vast_cmd(["show", "instance", str(instance_id), "--raw"])
        if not isinstance(info, dict):
            return f"Failed to get instance info: {info}"
        ssh_host = info.get("ssh_host")
        ssh_port = info.get("ssh_port")
        if not ssh_host or not ssh_port:
            return f"No SSH info for instance {instance_id}"
        ssh_key = os.path.expanduser("~/.ssh/id_vastai")
        if not os.path.exists(ssh_key):
            ssh_key = os.path.expanduser("~/.ssh/id_rsa")
        cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15",
            "-i", ssh_key,
            "-p", str(ssh_port),
            f"root@{ssh_host}",
            remote_cmd,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode != 0:
            return f"SSH failed (exit {result.returncode}):\nstdout: {stdout}\nstderr: {stderr}"
        return stdout
    except Exception as exc:
        return f"SSH error: {exc}"


@tool
def search_gpu_offers(
    min_vram_gb: int = 48,
    max_price_per_hour: float = 5.0,
    min_reliability: float = 0.90,
    min_disk_gb: int = 120,
    gpu_name_filter: str = "",
) -> str:
    """Search Vast.ai for GPU offers matching requirements."""
    vram_mb = min_vram_gb
    query_parts = [
        f"gpu_ram>={vram_mb}",
        f"dph_total<={max_price_per_hour}",
        "rentable=true",
        f"reliability>{min_reliability}",
        f"disk_space>={min_disk_gb}",
    ]
    if gpu_name_filter:
        query_parts.insert(0, f"gpu_name={gpu_name_filter}")
    query = " ".join(query_parts)

    try:
        raw = _vast_cmd([
            "search", "offers",
            "--type", "on-demand",
            "--order", "dph_total+",
            "--raw",
            query,
        ])
        offers = raw if isinstance(raw, list) else []
    except Exception as exc:
        return json.dumps({"error": str(exc), "offers": []})

    simplified = []
    for o in offers[:20]:
        vram = o.get("gpu_ram", 0)
        vram_gb = round(vram / 1024, 1) if vram else 0
        simplified.append({
            "offer_id": o.get("id"),
            "gpu_name": o.get("gpu_name"),
            "vram_gb": vram_gb,
            "price_per_hour": o.get("dph_total"),
            "reliability": o.get("reliability"),
            "disk_gb": o.get("disk_space"),
            "location": o.get("geolocation"),
        })

    return json.dumps({
        "query": query,
        "count": len(simplified),
        "offers": simplified,
    }, indent=2)


@tool
def provision_specific_offer(
    offer_id: int,
    disk_gb: int = 150,
    docker_image: str = "pytorch/pytorch:2.10.0-cuda12.6-cudnn9-runtime",
    label: str = "documentary-ltx",
    mode: str = "auto",
) -> str:
    """Provision a specific Vast.ai offer and auto-start the worker.

    The onstart script is dead simple: clone repo, start worker.
    API keys are configured afterward via SSH.
    """
    label_lower = label.lower()
    if mode == "auto":
        if "tts" in label_lower:
            mode = "tts"
        elif "ltx" in label_lower or "video" in label_lower:
            mode = "ltx"
        else:
            mode = "both"

    # Dead simple onstart. Install runtime deps, clone correct branch, start worker.
    # Models are downloaded lazily by the worker's background bootstrap thread.
    onstart_cmd = (
        f"cd /workspace && "
        f"apt-get update -qq && apt-get install -y -qq git curl wget ffmpeg && "
        f"pip install -q fastapi uvicorn soundfile && "
        f"git clone --depth 1 --branch strands-migration https://github.com/OrpingtonClose/economy-documentary.git repo && "
        f"nohup python repo/scripts/gpu_worker.py --mode {mode} --port 8880 > /workspace/worker.log 2>&1 & "
        f"echo started"
    )

    try:
        result = _vast_cmd([
            "create", "instance",
            str(offer_id),
            "--image", docker_image,
            "--disk", str(disk_gb),
            "--ssh",
            "--direct",
            "--env", "-p 8880:8880",
            "--label", label,
            "--onstart-cmd", onstart_cmd,
        ])
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)})

    instance_id = None
    if isinstance(result, dict):
        instance_id = result.get("new_contract")
    elif isinstance(result, str) and "new_contract" in result:
        import re
        m = re.search(r"'new_contract'\s*:\s*(\d+)", result)
        if m:
            instance_id = int(m.group(1))

    return json.dumps({
        "status": "created" if instance_id else "unknown",
        "instance_id": instance_id,
        "offer_id": offer_id,
    }, indent=2)


@tool
def check_instance_status(instance_id: int) -> str:
    """Check the status of a Vast.ai instance."""
    try:
        result = _vast_cmd(["show", "instance", str(instance_id), "--raw"])
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)})

    if not isinstance(result, dict):
        return json.dumps({"status": "error", "raw": str(result)})

    status = result.get("actual_status", result.get("status_msg", "unknown"))
    ports = result.get("ports", {}) or {}
    port_bindings = []
    for k, v in ports.items():
        port_bindings.append({"port": k, "binding": v})

    return json.dumps({
        "instance_id": instance_id,
        "status": status,
        "public_ipaddr": result.get("public_ipaddr"),
        "ssh_host": result.get("ssh_host"),
        "ssh_port": result.get("ssh_port"),
        "port_bindings": port_bindings,
        "gpu_name": result.get("gpu_name"),
    }, indent=2)


@tool
def destroy_instance(instance_id: int) -> str:
    """Destroy a Vast.ai instance."""
    try:
        _vast_cmd(["destroy", "instance", str(instance_id)])
        return json.dumps({"status": "destroyed", "instance_id": instance_id})
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)})


@tool
def set_gpu_worker_url(worker_url: str) -> str:
    """Register a VIDEO worker URL with the WorkerProvisioner singleton.

    After provisioning a Vast.ai instance for video generation (LTX),
    call this with the worker URL.
    """
    import time
    from worker_provisioner import get_provisioner, check_worker_health
    healthy = False
    for attempt in range(1, 61):
        healthy = check_worker_health(worker_url, "video")
        if healthy:
            break
        time.sleep(10)
    if not healthy:
        return json.dumps({
            "status": "warning",
            "role": "video",
            "worker_url": worker_url,
            "warning": "Worker health check never passed — registered anyway but may fail",
        })
    provisioner = get_provisioner()
    provisioner.register_worker(worker_url, "video")
    return json.dumps({
        "status": "set",
        "role": "video",
        "worker_url": worker_url,
    })


@tool
def set_tts_worker_url(worker_url: str) -> str:
    """Register a TTS worker URL with the WorkerProvisioner singleton."""
    import time
    from worker_provisioner import get_provisioner, check_worker_health
    healthy = False
    for attempt in range(1, 61):
        healthy = check_worker_health(worker_url, "tts")
        if healthy:
            break
        time.sleep(10)
    if not healthy:
        return json.dumps({
            "status": "warning",
            "role": "tts",
            "worker_url": worker_url,
            "warning": "Worker health check never passed — registered anyway but may fail",
        })
    provisioner = get_provisioner()
    provisioner.register_worker(worker_url, "tts")
    return json.dumps({
        "status": "set",
        "role": "tts",
        "worker_url": worker_url,
    })


@tool
def ssh_run_command(instance_id: int, command: str) -> str:
    """Run a shell command on a Vast.ai instance via SSH.

    Use this to troubleshoot workers: check process status, read logs,
    install packages, or restart services.
    """
    return _ssh_cmd(instance_id, command)


@tool
def get_registered_workers() -> str:
    """Return all currently registered worker URLs.

    ALWAYS call this BEFORE provisioning a new worker.
    If a URL is returned for your role, use it directly —
    do NOT provision a duplicate.
    """
    try:
        from worker_provisioner import get_provisioner
        wp = get_provisioner()
        return json.dumps({
            "video_urls": wp.get_worker_urls("video"),
            "tts_urls": wp.get_worker_urls("tts"),
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def run_vast_cli(command: str) -> str:
    """Run an arbitrary vastai CLI command and return the output."""
    raw_key = os.environ.get("VAST_AI_KEY", "") or os.environ.get("VAST_API_KEY", "")
    api_key = raw_key.split()[0].strip() if raw_key else ""
    if not api_key:
        env = _critical_env()
        raw_key = env.get("VAST_AI_KEY", "") or env.get("VAST_API_KEY", "")
        api_key = raw_key.split()[0].strip() if raw_key else ""
    if not api_key:
        return json.dumps({"status": "error", "error": "VAST_API_KEY not set"})
    cmd = ["vastai", "--api-key", api_key] + command.split()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=_critical_env())
        output = result.stdout.strip()
        if result.stderr.strip():
            output += "\n[stderr]:\n" + result.stderr.strip()
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output or "(no output)"
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)})


@tool
def run_bash_command(command: str) -> str:
    """Run an arbitrary bash command on the local machine."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            env=_critical_env(),
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        output = ""
        if stdout:
            output += stdout
        if stderr:
            output += "\n[stderr]:\n" + stderr
        return output or f"Exit code: {result.returncode}"
    except Exception as exc:
        return f"Bash error: {exc}"


def _critical_env() -> dict[str, str]:
    env = dict(os.environ)
    env_path = os.path.expanduser("~/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    if key not in env or not env[key]:
                        env[key] = val.strip().strip('"').strip("'")
    return env
