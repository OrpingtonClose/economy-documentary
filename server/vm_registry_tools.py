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
from vm_registry import get_vm, list_vms_for_run, record_health_check

logger = logging.getLogger(__name__)


@tool
def query_vm_registry(run_id: str, stage: str = "") -> str:
    """Query the VM registry for VMs associated with a run.

    Returns a plain-text summary of all VMs, their status, worker readiness,
    and SSH connection details. Use this BEFORE provisioning a new VM to
    check if one already exists.
    """
    vms = list_vms_for_run(run_id)
    if not vms:
        return f"No VMs recorded for run '{run_id}'."

    lines = [f"VM Registry for run '{run_id}':"]
    for vm in vms:
        if stage and vm.labeled_for_stage != stage:
            continue
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
        with urllib.request.urlopen(url, timeout=10) as resp:
            raw_text = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
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
def get_provisioning_guidance(run_id: str, stage: str, agent_reasoning: str) -> str:
    """Get system-corrected guidance for VM provisioning.

    Tell the system what you're thinking (e.g. "SSH failed, I should provision
    a new VM") and it will check the registry and tell you what to ACTUALLY do.

    This prevents the SSH death spiral by grounding agent reasoning in reality.
    """
    from vm_registry import decide_provisioning_action

    decision = decide_provisioning_action(agent_reasoning, run_id, stage)

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
