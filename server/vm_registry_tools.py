"""Agent-facing tools for VM registry — extracted text reasoning over typed state.

These tools are given TO THE AGENT so it can query VM state without doing
raw text parsing. The agent still reasons in text, but the tools it calls
return structured information.

This is the "types implied in text" architecture at the agent boundary:

    Agent (text reasoning) → calls tool → system extracts types →
    returns text summary → Agent (text reasoning)

The key insight: the agent never sees raw JSON or typed objects directly.
It sees TEXT summaries that were DERIVED from typed objects.
"""

from __future__ import annotations

import logging

from strands import tool
from vm_registry import get_vm, list_vms, record_health_check, record_provisioning

logger = logging.getLogger(__name__)


@tool
def query_vm_registry(stage: str = "") -> str:
    """Query the VM registry for VMs.

    Returns a plain-text summary of all VMs, their status, worker readiness,
    and SSH connection details. Use this BEFORE provisioning a new VM to
    check if one already exists.
    """
    vms = list_vms(stage)
    if not vms:
        return "No VMs recorded." if not stage else f"No VMs recorded for stage '{stage}'."

    lines = ["VM Registry:"]
    for vm in vms:
        ready = vm.worker_status.ready if vm.worker_status else "unknown"
        worker_type = vm.worker_status.worker_type if vm.worker_status else "unknown"
        lines.append(
            f"- ID {vm.instance_id}: status={vm.status}, stage={vm.labeled_for_stage}, "
            f"ssh={vm.ssh_host}:{vm.ssh_port}, gpu={vm.gpu_name}, "
            f"worker_ready={ready}, worker_type={worker_type}, "
            f"price=${vm.price_per_hour}/hr"
        )

    return "\n".join(lines)


@tool
def check_worker_health(instance_id: str, worker_url: str = "") -> str:
    """Check if a worker VM is healthy and ready for jobs.

    Performs HTTP GET to the worker URL and returns a plain-text status.
    If worker_url is not provided, looks up the registry for the instance.
    """
    import urllib.request

    vm = get_vm(instance_id)
    if vm is None:
        return f"VM {instance_id} not found in registry."

    url = worker_url or vm.worker_url
    if not url:
        # Try to construct from SSH info
        if vm.ssh_host:
            url = f"http://{vm.ssh_host}:8880/"
        else:
            return f"VM {instance_id} has no worker URL or SSH info."

    try:
        with urllib.request.urlopen(url) as resp:
            raw_text = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        from maintainer import notify_maintainer
        notify_maintainer(
            operation="worker_health_check",
            error=str(exc),
            context={"instance_id": instance_id, "url": url},
        )
        return f"Worker at {url} is NOT reachable: {exc}"

    # Extract typed status from raw HTTP text
    status = record_health_check(instance_id, raw_text)

    if status.ready:
        return (
            f"Worker {instance_id} at {url} is READY.\n"
            f"Type: {status.worker_type}, GPU: {status.gpu_name}, "
            f"VRAM: {status.vram_used_gb:.1f}/{status.vram_total_gb:.1f}GB, "
            f"Queue: {status.jobs_in_queue} jobs, Uptime: {status.uptime_seconds:.0f}s"
        )
    else:
        return (
            f"Worker {instance_id} at {url} is NOT ready.\n"
            f"Type: {status.worker_type}, Status: still loading or error.\n"
            f"Raw response preview: {raw_text[:200]}"
        )


@tool
def get_provisioning_guidance(stage: str, agent_reasoning: str) -> str:
    """Get system-corrected guidance for VM provisioning.

    Tell the system what you're thinking (e.g. "SSH failed, I should provision
    a new VM") and it will check the registry and tell you what to ACTUALLY do.

    This prevents the SSH death spiral by grounding agent reasoning in reality.
    """
    from vm_registry import decide_provisioning_action

    decision = decide_provisioning_action(agent_reasoning, stage)

    if decision.action == "use_existing":
        return (
            f"GUIDANCE: Use existing VM {decision.target_instance_id}.\n"
            f"Reason: {decision.reason}\n"
            f"Your reasoning was grounded by the registry. Do NOT provision a new VM."
        )
    elif decision.action == "provision_new":
        return (
            f"GUIDANCE: Provision a new VM.\n"
            f"Reason: {decision.reason}\n"
            f"No suitable existing VM found."
        )
    elif decision.action == "destroy_and_reprovision":
        return (
            f"GUIDANCE: Destroy VM {decision.target_instance_id} and reprovision.\n"
            f"Reason: {decision.reason}\n"
            f"The existing VM is confirmed dead."
        )
    elif decision.action == "wait":
        return (
            f"GUIDANCE: WAIT.\n"
            f"Reason: {decision.reason}\n"
            f"The VM is still loading. Check again in 30 seconds."
        )
    else:
        return (
            f"GUIDANCE: {decision.action.upper()}.\n"
            f"Reason: {decision.reason}\n"
            f"Confidence: {decision.confidence:.0%}"
        )


@tool
def record_vm(stage: str, raw_cli_output: str) -> str:
    """Record a newly provisioned VM in the registry.

    After calling bash_command("vastai create instance ...") you get raw
    output. Then call bash_command("vastai show instance <id> --raw") to get
    full details. Pass that full output here so the system can extract SSH
    host, port, GPU info, and store it.

    Args:
        stage: 'audio' or 'video' — which stage this VM serves.
        raw_cli_output: Raw text output from "vastai show instance <id> --raw".
    """
    vm = record_provisioning(raw_cli_output, stage)
    return (
        f"Recorded VM {vm.instance_id} for stage={stage}.\n"
        f"Status: {vm.status}, SSH: {vm.ssh_host}:{vm.ssh_port}, "
        f"GPU: {vm.gpu_name}, VRAM: {vm.vram_gb}GB, "
        f"Price: ${vm.price_per_hour}/hr"
    )


@tool
def update_vm_worker_url(instance_id: str, worker_url: str) -> str:
    """Update the worker URL for a VM in the registry.

    Use this after starting a worker so dispatch tools can find it.

    Args:
        instance_id: Vast.ai instance ID.
        worker_url: HTTP endpoint of the worker, e.g. "http://1.2.3.4:8880/".
    """
    from vm_registry import update_worker_url as _update

    changed = _update(instance_id, worker_url)
    if changed:
        return f"Updated VM {instance_id} worker_url to {worker_url}."
    return f"VM {instance_id} not found — no update made."
