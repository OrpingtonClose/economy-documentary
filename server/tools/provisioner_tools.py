"""Agent-facing tools for the provisioner agent.

The provisioner agent:
  1. Claims pending jobs from the queue
  2. Provisions VMs via Vast.ai
  3. Assigns jobs to workers
  4. Monitors worker health
  5. Marks jobs completed/failed

It NEVER talks to media agents directly. The queue is the only coordination mechanism.
"""

from __future__ import annotations

from strands import tool

from job_queue import (
    claim_next_pending_job,
    mark_job_completed,
    mark_job_failed,
    mark_job_running,
)



# ---------------------------------------------------------------------------
# Queue reading
# ---------------------------------------------------------------------------

@tool
def claim_job(stage: str) -> str:
    """Claim the next pending job for a stage. Returns job details or 'No jobs available.'

    stage: 'audio' or 'video'
    """
    job = claim_next_pending_job(stage)
    if job is None:
        return "No jobs available."

    comments = " | ".join(job.qa_comments) if job.qa_comments else "none"
    return (
        f"Claimed job {job.job_id}\n"
        f"  type: {job.job_type.value}\n"
        f"  run_id: {job.run_id}\n"
        f"  scene: {job.scene_num}\n"
        f"  attempts: {job.attempts}/{job.max_attempts}\n"
        f"  qa_comments: {comments}\n"
        f"  payload: {job.payload}"
    )


@tool
def set_job_running(job_id: str, worker_id: str) -> str:
    """Mark a job as running on a specific worker."""
    mark_job_running(job_id, worker_id)
    return f"Job {job_id} marked running on worker {worker_id}"


@tool
def set_job_completed(job_id: str, b2_artifact_key: str) -> str:
    """Mark a job as completed after the worker uploads the artifact to B2."""
    mark_job_completed(job_id, b2_artifact_key)
    return f"Job {job_id} completed. Artifact at B2 key: {b2_artifact_key}"


@tool
def set_job_failed(job_id: str, error_message: str) -> str:
    """Mark a job as failed (permanent or retryable depending on attempts)."""
    mark_job_failed(job_id, error_message)
    return f"Job {job_id} marked failed: {error_message}"


# ---------------------------------------------------------------------------
# VM provisioning wrappers (thin wrappers around vast CLI)
# ---------------------------------------------------------------------------

@tool
def vast_search_offers(gpu_min_vram: float = 0, query: str = "") -> str:
    """Search Vast.ai for GPU offers.

    gpu_min_vram: minimum VRAM in GB (e.g. 24.0)
    query: extra search terms (e.g. 'RTX 4090')
    """
    from strands_agents.shared_a2a.vast_provisioning import run_vast_cli

    cmd = "vastai search offers --type on-demand --raw"
    if gpu_min_vram:
        cmd += f" 'gpu_ram>={int(gpu_min_vram * 1024)}'"
    if query:
        cmd += f" {query}"
    result = run_vast_cli(cmd)
    return result


@tool
def vast_create_instance(offer_id: str, image: str, disk_gb: int = 64) -> str:
    """Provision a Vast.ai instance from an offer ID.

    offer_id: the numeric offer ID from vast_search_offers
    image: Docker image (e.g. 'pytorch/pytorch:2.10.0-cuda12.6-cudnn9-runtime')
    disk_gb: disk size in GB
    """
    from strands_agents.shared_a2a.vast_provisioning import run_vast_cli

    cmd = (
        f"vastai create instance {offer_id} "
        f"--image {image} --disk {disk_gb} --ssh --direct --env '-p 8880:8880'"
    )
    return run_vast_cli(cmd)


@tool
def vast_show_instance(instance_id: str) -> str:
    """Get details of a Vast.ai instance."""
    from strands_agents.shared_a2a.vast_provisioning import run_vast_cli

    return run_vast_cli(f"vastai show instance {instance_id} --raw")


@tool
def vast_destroy_instance(instance_id: str) -> str:
    """Destroy a Vast.ai instance."""
    from strands_agents.shared_a2a.vast_provisioning import run_vast_cli

    return run_vast_cli(f"vastai destroy instance {instance_id}")


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------

@tool
def registry_query(run_id: str, stage: str = "") -> str:
    """Query the VM registry for VMs associated with a run.

    Returns plain-text summary of VMs, their status, and worker readiness.
    """
    from vm_registry_tools import query_vm_registry

    return query_vm_registry(run_id, stage)


@tool
def registry_check_health(instance_id: str, worker_url: str = "") -> str:
    """Check if a worker VM is healthy.

    Returns plain-text status. If not reachable, reports why.
    """
    from vm_registry_tools import check_worker_health

    return check_worker_health(instance_id, worker_url)
