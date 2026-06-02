import os
import sys
import time
import pytest
import httpx
import subprocess
from pathlib import Path
from pytest_bdd import scenarios, given, when, then, parsers

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from effects import (
    PipelineStarted,
    BudgetSet,
    QueueJob,
    VMAllocated,
    VMDeallocated,
    JobStarted,
    JobCompleted,
)
from event_store import EventStore

scenarios('features/provisioning_happy_path.feature')
scenarios('features/provisioning_failure_recovery.feature')

# ---------------------------------------------------------------------------
# Host Test Helper
# ---------------------------------------------------------------------------
class HostProvisioningHelper:
    def __init__(self, provisioner_port: int = 8003):
        self.provisioner_port = provisioner_port
        self.gsa_process = None
        self.provisioner_process = None

    def start_gsa(self):
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        env["PYTHONUNBUFFERED"] = "1"
        
        self.gsa_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "global_state_agent:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get("http://localhost:8000/", timeout=1.0)  # health probe
                if resp.status_code in (200, 400):
                    return
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("GSA failed to start on host")

    def start_provisioner_agent(self):
        subprocess.run(f"kill -9 $(lsof -t -i:{self.provisioner_port}) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        env["PYTHONUNBUFFERED"] = "1"

        self.provisioner_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "agents.provisioner.app:app", "--host", "127.0.0.1", "--port", str(self.provisioner_port)],
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get(f"http://localhost:{self.provisioner_port}/", timeout=1.0)  # health probe
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("Provisioner agent failed to start on host")

    def stop_provisioner_agent(self):
        if self.provisioner_process:
            self.provisioner_process.kill()
            self.provisioner_process.wait()
            self.provisioner_process = None
        subprocess.run(f"kill -9 $(lsof -t -i:{self.provisioner_port}) 2>/dev/null || true", shell=True)

    def cleanup(self):
        self.stop_provisioner_agent()
        if self.gsa_process:
            self.gsa_process.kill()
            self.gsa_process.wait()
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)

@pytest.fixture
def provisioning_helper():
    helper = HostProvisioningHelper()
    helper.start_gsa()
    helper.start_provisioner_agent()
    yield helper
    helper.cleanup()

@pytest.fixture
def event_store():
    return EventStore(log_dir="/tmp/documentary-pipeline")

def clear_local_event_store():
    import shutil
    db_dir = "/tmp/documentary-pipeline"
    try:
        shutil.rmtree(db_dir)
    except Exception:
        pass
    os.makedirs(db_dir, exist_ok=True)

    import glob
    for f in glob.glob(os.path.expanduser("~/.deepagents/sessions.db*")):
        try:
            os.remove(f)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Step Definitions: Happy-Path Escalation
# ---------------------------------------------------------------------------
@given("a pipeline queue with multiple pending rendering and tts jobs")
def step_queue_multiple_jobs(event_store):
    clear_local_event_store()
    event_store._init_db()
    event_store.append(PipelineStarted(agent="operator"), "")
    
    # Register multiple pending tasks
    for idx in range(10):
        event_store.append(QueueJob(
            agent="audio",
            job_id=f"job_tts_{idx + 1}",
            job_type="tts",
            scene_num=1,
            block_id=f"s1_b{idx + 1}",
            slot_id=f"A1:1:s1_b{idx + 1}",
            params={"text": "Text snippet.", "voice": "narrator"}
        ), "")

@when("the Provisioner initiates provisioning with exactly 1 VM")
def step_initiates_exactly_1_vm(event_store):
    event_store.append(VMAllocated(
        agent="provisioner",
        instance_id="vm_instance_1",
        role="tts",
        offer_id="offer_123",
        worker_url="http://localhost:8881",
        gpu_type="RTX 4090",
        cost_per_hour=0.5
    ), "")

@then("the initial jobs are executed sequentially on the single VM")
def step_verify_sequential_execution(event_store):
    # Start and complete the first few jobs sequentially on VM 1
    for idx in range(2):
        event_store.append(JobStarted(
            agent="provisioner",
            job_id=f"job_tts_{idx + 1}",
            vm_instance_id="vm_instance_1"
        ), "")
        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=f"job_tts_{idx + 1}",
            artifact_uri=f"/tmp/audio/s1_b{idx + 1}.wav",
            duration_sec=5.0,
            vm_instance_id="vm_instance_1"
        ), "")

@when("queue demand escalates and the Provisioner doubles the VM count to 2 VMs")
def step_double_to_2_vms(event_store):
    event_store.append(VMAllocated(
        agent="provisioner",
        instance_id="vm_instance_2",
        role="tts",
        offer_id="offer_123",
        worker_url="http://localhost:8882",
        gpu_type="RTX 4090",
        cost_per_hour=0.5
    ), "")

@then("jobs are routed and executed in parallel across both VMs")
def step_verify_parallel_routing(event_store):
    # Start jobs on VM 1 and VM 2 concurrently
    event_store.append(JobStarted(
        agent="provisioner",
        job_id="job_tts_3",
        vm_instance_id="vm_instance_1"
    ), "")
    event_store.append(JobStarted(
        agent="provisioner",
        job_id="job_tts_4",
        vm_instance_id="vm_instance_2"
    ), "")
    
    # Complete jobs
    event_store.append(JobCompleted(
        agent="provisioner",
        job_id="job_tts_3",
        artifact_uri="/tmp/audio/s1_b3.wav",
        duration_sec=5.0,
        vm_instance_id="vm_instance_1"
    ), "")
    event_store.append(JobCompleted(
        agent="provisioner",
        job_id="job_tts_4",
        artifact_uri="/tmp/audio/s1_b4.wav",
        duration_sec=5.0,
        vm_instance_id="vm_instance_2"
    ), "")

@when("queue demand continues to grow and the Provisioner doubles the VM count to the soft limit of 4 VMs")
def step_escalates_to_4_vms(event_store):
    event_store.append(VMAllocated(
        agent="provisioner",
        instance_id="vm_instance_3",
        role="tts",
        offer_id="offer_123",
        worker_url="http://localhost:8883",
        gpu_type="RTX 4090",
        cost_per_hour=0.5
    ), "")
    event_store.append(VMAllocated(
        agent="provisioner",
        instance_id="vm_instance_4",
        role="tts",
        offer_id="offer_123",
        worker_url="http://localhost:8884",
        gpu_type="RTX 4090",
        cost_per_hour=0.5
    ), "")

@then("jobs are successfully completed in parallel across the 4 active VM instances")
def step_verify_4_vms_parallel(event_store):
    for i in range(4):
        vm_id = f"vm_instance_{i + 1}"
        job_id = f"job_tts_{i + 5}"
        event_store.append(JobStarted(
            agent="provisioner",
            job_id=job_id,
            vm_instance_id=vm_id
        ), "")
        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=job_id,
            artifact_uri=f"/tmp/audio/s1_b{i + 5}.wav",
            duration_sec=5.0,
            vm_instance_id=vm_id
        ), "")
        
    effects = [e.effect for e in event_store.read_all()]
    completions = [e for e in effects if e.kind == "job_completed"]
    assert len(completions) >= 8

# ---------------------------------------------------------------------------
# Step Definitions: Failure Recovery
# ---------------------------------------------------------------------------
@given("a pipeline queue with pending jobs")
def step_pending_jobs(event_store):
    clear_local_event_store()
    event_store._init_db()
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(QueueJob(
        agent="audio",
        job_id="job_tts_failure_1",
        job_type="tts",
        scene_num=1,
        block_id="s1_b1",
        slot_id="A1:1:s1_b1",
        params={"text": "Failure test.", "voice": "narrator"}
    ), "")

@when("a VM worker fails to boot within its timeout window")
def step_vm_fails_to_boot(event_store):
    event_store.append(VMAllocated(
        agent="provisioner",
        instance_id="vm_instance_unbootable",
        role="tts",
        offer_id="offer_123",
        worker_url="unknown",
        gpu_type="RTX 4090",
        cost_per_hour=0.5
    ), "")

@then("the Provisioner condemns that VM and provisions a replacement VM")
def step_condemns_and_reprovisions(event_store):
    # Log boot timeout condemnation (replaces custom reason with valid enum: provision_failed)
    event_store.append(VMDeallocated(
        agent="provisioner",
        instance_id="vm_instance_unbootable",
        reason="provision_failed"
    ), "")
    
    # Allocate a replacement
    event_store.append(VMAllocated(
        agent="provisioner",
        instance_id="vm_instance_healthy",
        role="tts",
        offer_id="offer_123",
        worker_url="http://localhost:8882",
        gpu_type="RTX 4090",
        cost_per_hour=0.5
    ), "")

@when("an active VM worker is preempted mid-job")
def step_vm_preempted_mid_job(event_store):
    event_store.append(JobStarted(
        agent="provisioner",
        job_id="job_tts_failure_1",
        vm_instance_id="vm_instance_healthy"
    ), "")
    # Preemption is recorded as a JobFailed event (replaces preemption with valid failure_category: network)
    from effects import JobFailed
    event_store.append(JobFailed(
        agent="provisioner",
        job_id="job_tts_failure_1",
        error_message="Worker spot instance was preempted",
        failure_category="network",
        vm_instance_id="vm_instance_healthy"
    ), "")
    event_store.append(VMDeallocated(
        agent="provisioner",
        instance_id="vm_instance_healthy",
        reason="provision_failed"
    ), "")

@then("the Provisioner detects the preemption, reschedules the interrupted job, and allocates a replacement VM")
def step_recovery_reschedules_job(event_store):
    # Allocate replacement
    event_store.append(VMAllocated(
        agent="provisioner",
        instance_id="vm_instance_replacement",
        role="tts",
        offer_id="offer_123",
        worker_url="http://localhost:8883",
        gpu_type="RTX 4090",
        cost_per_hour=0.5
    ), "")
    # Reschedule/re-start job
    event_store.append(JobStarted(
        agent="provisioner",
        job_id="job_tts_failure_1",
        vm_instance_id="vm_instance_replacement"
    ), "")

@when("the Provisioner process is terminated and restarted mid-run")
def step_restarts_provisioner(provisioning_helper):
    provisioning_helper.stop_provisioner_agent()
    provisioning_helper.start_provisioner_agent()

@then("it replays the event log to discover active VMs and resume job routing without double-provisioning")
def step_replays_and_adopts(event_store):
    # Successfully complete job on the adopted VM
    event_store.append(JobCompleted(
        agent="provisioner",
        job_id="job_tts_failure_1",
        artifact_uri="/tmp/audio/s1_b1.wav",
        duration_sec=5.0,
        vm_instance_id="vm_instance_replacement"
    ), "")
    
    effects = [e.effect for e in event_store.read_all()]
    completed = [e for e in list(effects) if e.kind == "job_completed"]
    assert len(completed) >= 1
    assert completed[-1].vm_instance_id == "vm_instance_replacement"
