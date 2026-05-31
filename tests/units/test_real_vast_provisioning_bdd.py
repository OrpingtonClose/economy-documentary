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
    JobCompleted,
)
from event_store import EventStore

scenarios('features/real_vast_provisioning.feature')

class HostTestHelper:
    def __init__(self, agent_port: int = 8003):
        self.agent_port = agent_port
        self.gsa_process = None
        self.agent_process = None
        self.api_key = None
        self.allocated_instance_ids = []

        # Read VAST_API_KEY from server/.env
        env_path = PROJECT_ROOT / "server" / ".env"
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    if line.startswith("VAST_API_KEY="):
                        self.api_key = line.split("=")[1].strip()

    def start_gsa(self):
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/Users/orpington/documentary-pipeline"

        self.gsa_process = subprocess.Popen(
            [str(PROJECT_ROOT / "server/.venv/bin/uvicorn"), "global_state_agent:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        
        # Wait for GSA
        for _ in range(15):
            try:
                resp = httpx.get("http://localhost:8000/", timeout=1.0)  # health check
                if resp.status_code in (200, 400):
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("GSA failed to start on host")

    def start_provisioner_agent(self):
        subprocess.run(f"kill -9 $(lsof -t -i:{self.agent_port}) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/Users/orpington/documentary-pipeline"
        env["INTEGRATION_TESTS"] = "1"
        if self.api_key:
            env["VAST_API_KEY"] = self.api_key

        self.agent_process = subprocess.Popen(
            [str(PROJECT_ROOT / "server/.venv/bin/uvicorn"), "agents.provisioner.app:app", "--host", "127.0.0.1", "--port", str(self.agent_port)],
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )

        # Wait for agent
        for _ in range(15):
            try:
                resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)  # health check
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("Provisioner agent failed to start on host")

    def cleanup(self):
        if self.agent_process:
            self.agent_process.terminate()
            self.agent_process.wait()
        if self.gsa_process:
            self.gsa_process.terminate()
            self.gsa_process.wait()
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
        subprocess.run(f"kill -9 $(lsof -t -i:{self.agent_port}) 2>/dev/null || true", shell=True)

        # Teardown any real rented instances to avoid charges
        if self.allocated_instance_ids and self.api_key:
            for instance_id in self.allocated_instance_ids:
                print(f"Cleaning up real instance {instance_id}...")
                subprocess.run(
                    [str(PROJECT_ROOT / "server/.venv/bin/vastai"), "destroy", "instance", str(instance_id)],
                    env={"VAST_API_KEY": self.api_key},
                    input=b"y\n"
                )

@pytest.fixture(scope="module")
def host_helper():
    helper = HostTestHelper(agent_port=8003)
    helper.start_gsa()
    helper.start_provisioner_agent()
    yield helper
    helper.cleanup()

@pytest.fixture
def run_id():
    return f"run_{int(time.time() * 1000)}"

@pytest.fixture
def event_store():
    return EventStore(log_dir="/Users/orpington/documentary-pipeline")

def clear_local_event_store():
    import glob
    for file in glob.glob("/Users/orpington/documentary-pipeline/events_*.db*"):
        try:
            os.remove(file)
        except Exception:
            pass

@given('the GSA event store contains a queued "tts" job')
def step_queued_tts_job(run_id, event_store):
    clear_local_event_store()
    event_store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    event_store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=20.0), "")
    
    event_store.append(run_id, QueueJob(
        run_id=run_id,
        agent="audio",
        job_id="job_tts_real_1",
        job_type="tts",
        scene_num=1,
        block_id="s1_v1",
        slot_id="s1_v1",
        params={"text": "This is a real TTS provisioning BDD test.", "voice": "narrator"}
    ), "")

@given('the system budget has remaining funds')
def step_remaining_funds():
    pass

@given('the Provisioner Agent is configured for real Vast.ai cloud provisioning')
def step_configured_real_provisioning(host_helper):
    assert host_helper.api_key is not None, "VAST_API_KEY must be configured in server/.env"

@when('the Provisioner Agent is woken up')
def step_wake_provisioner(run_id, host_helper):
    resp = httpx.post(f"http://localhost:{host_helper.agent_port}/", json={
        "run_id": run_id,
        "notification_type": "instruction",
        "context": {}
    })
    assert resp.status_code == 200

@then('it should query available GPU offers on Vast.ai')
def step_verify_query_offers(run_id, event_store):
    pass

@then('it should select the cheapest suitable offer under $1.50 per hour')
def step_verify_select_offer(run_id, event_store):
    pass

@then('it should allocate a real instance on Vast.ai using the selected offer')
def step_allocate_real_instance():
    pass

@then('the GSA event store should contain a "vm_allocated" effect with the new instance ID')
def step_check_real_vm_allocated(run_id, event_store, host_helper):
    success = False
    instance_id = None
    for _ in range(120):
        effects = [e.effect for e in event_store.read_all(run_id)]
        kinds = [e.kind for e in effects]
        if "vm_allocated" in kinds:
            allocated_effect = next(e for e in effects if e.kind == "vm_allocated")
            instance_id = allocated_effect.instance_id
            if instance_id and instance_id != "vm_instance_1":
                success = True
                host_helper.allocated_instance_ids.append(instance_id)
                break
        time.sleep(2.0)
    assert success, "Real vm_allocated effect was not found in event store"

@then('we wait for the instance to transition to the running state')
def step_wait_for_running(host_helper):
    instance_id = host_helper.allocated_instance_ids[-1]
    running = False
    for _ in range(90):
        res = subprocess.run(
            [str(PROJECT_ROOT / "server/.venv/bin/vastai"), "show", "instances"],
            env={"VAST_API_KEY": host_helper.api_key},
            capture_output=True,
            text=True
        )
        if str(instance_id) in res.stdout:
            for line in res.stdout.splitlines():
                if str(instance_id) in line and "running" in line.lower():
                    running = True
                    break
        if running:
            break
        time.sleep(5.0)
    assert running, f"Instance {instance_id} failed to transition to running state"

@then('we verify the worker agent becomes healthy and responsive')
def step_verify_worker_healthy(host_helper, run_id, event_store):
    effects = [e.effect for e in event_store.read_all(run_id)]
    allocated_effect = next(e for e in effects if e.kind == "vm_allocated")
    worker_url = allocated_effect.worker_url
    
    healthy = False
    for _ in range(60):
        try:
            resp = httpx.get(f"{worker_url}/", timeout=2.0)  # health check probe
            if resp.status_code == 200:
                healthy = True
                break
        except Exception:
            pass
        time.sleep(5.0)
    assert healthy, f"Worker at {worker_url} did not report healthy"

@then('the Provisioner Agent dispatches the "tts" job to the worker')
def step_dispatch_job(run_id, host_helper, event_store):
    started = False
    for _ in range(30):
        effects = [e.effect for e in event_store.read_all(run_id)]
        if any(e.kind == "job_started" for e in effects):
            started = True
            break
        time.sleep(2.0)
    assert started, "TTS job was not successfully dispatched to the worker"

@then('the worker should process the job and produce a narration audio artifact')
def step_worker_processes_job():
    pass

@then('the GSA event store should contain a "job_completed" effect')
def step_wait_job_completion(run_id, event_store):
    completed = False
    for _ in range(90):
        effects = [e.effect for e in event_store.read_all(run_id)]
        if any(e.kind == "job_completed" for e in effects):
            completed = True
            break
        time.sleep(5.0)
    assert completed, "GSA event store did not receive a job_completed effect"

@then('the generated audio file must be downloadable from the worker and have a non-zero size')
def step_download_and_verify_artifact(run_id, event_store):
    effects = [e.effect for e in event_store.read_all(run_id)]
    completed_effect = next(e for e in effects if e.kind == "job_completed")
    artifact_uri = completed_effect.artifact_uri
    assert artifact_uri.startswith("http"), f"Artifact URI must be a valid HTTP URL: {artifact_uri}"
    
    resp = httpx.get(artifact_uri)
    assert resp.status_code == 200, f"Failed to download artifact from {artifact_uri}"
    assert len(resp.content) > 0, "Downloaded media artifact has 0 bytes"

@then('the Provisioner Agent should deallocate the active VM after job completion')
def step_trigger_deallocation(run_id, host_helper):
    resp = httpx.post(f"http://localhost:{host_helper.agent_port}/", json={
        "run_id": run_id,
        "notification_type": "instruction",
        "context": {}
    })
    assert resp.status_code == 200

@then('the GSA event store should contain a "vm_deallocated" effect with reason "job_done"')
def step_verify_deallocated(run_id, event_store, host_helper):
    deallocated = False
    for _ in range(30):
        effects = [e.effect for e in event_store.read_all(run_id)]
        deallocs = [e for e in effects if e.kind == "vm_deallocated"]
        if deallocs and deallocs[-1].reason == "job_done":
            deallocated = True
            break
        time.sleep(2.0)
    assert deallocated, "Provisioner did not deallocate the VM after job completion"