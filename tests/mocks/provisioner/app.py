"import sys
import time
from pathlib import Path

# Add tests/mocks directory to Python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from mock_base import create_mock_agent_app
from effects import VMAllocated, JobStarted, JobCompleted, VMDeallocated

async def provisioner_turn(run_id, state, event_store):
    jobs = state.get("jobs", {}).get("jobs", {})
    vms = state.get("vms", {}).get("vms", {})
    
    pending_jobs = [j for j in jobs.values() if j["status"] == "pending"]
    active_vms = [v for v in vms.values() if v["status"] == "active"]
    
    if pending_jobs:
        job = pending_jobs[0]
        # Find active VM for the job's role (job_type)
        matching_vm = next((v for v in active_vms if v["role"] == job["job_type"]), None)
        
        if not matching_vm:
            return VMAllocated(
                run_id=run_id,
                agent="provisioner",
                instance_id="vm_instance_1",
                role=job["job_type"],
                offer_id="1234",
                worker_url="http://localhost:8880",
                gpu_type="RTX 4090",
                cost_per_hour=0.45
            )
        else:
            return JobStarted(
                run_id=run_id,
                agent="provisioner",
                job_id=job["job_id"],
                vm_instance_id=matching_vm["instance_id"]
            )
    else:
        # Check running jobs
        running_jobs = [j for j in jobs.values() if j["status"] == "running"]
        if running_jobs:
            job = running_jobs[0]
            artifact_uri = "/tmp/audio.wav" if job["job_type"] == "tts" else "/tmp/video.mp4"
            return JobCompleted(
                run_id=run_id,
                agent="provisioner",
                job_id=job["job_id"],
                artifact_uri=artifact_uri,
                duration_sec=6.8,
                vm_instance_id=job["vm_instance_id"]
            
<truncated 523 bytes>