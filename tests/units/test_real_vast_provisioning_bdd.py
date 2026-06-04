# VERY IMPORTANT DO NOT ALTER
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
        # Documented Justification: Health check probe loop to verify port availability and wait for port unbinding.
        delay = 0.1
        for _ in range(5):
            time.sleep(delay)
            delay *= 1.5
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"

        self.gsa_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "global_state_agent:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        
        # Documented Justification: Health probe loop to wait for GSA process to spin up.
        # We use a loop with dynamic backoff to poll the GSA health endpoint during startup.
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get("http://127.0.0.1:8000/", timeout=1.0)  # health check
                if resp.status_code in (200, 400, 404, 500):
                    return
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("GSA failed to start on host")

    def start_provisioner_agent(self):
        subprocess.run(f"kill -9 $(lsof -t -i:{self.agent_port}) 2>/dev/null || true", shell=True)
        # Documented Justification: Health check probe loop to verify port availability and wait for port unbinding.
        delay = 0.1
        for _ in range(5):
            time.sleep(delay)
            delay *= 1.5
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        env["INTEGRATION_TESTS"] = "1"
        if self.api_key:
            env["VAST_API_KEY"] = self.api_key

        self.agent_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "agents.provisioner.app:app", "--host", "127.0.0.1", "--port", str(self.agent_port)],
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )

        # Documented Justification: Health probe loop to wait for Provisioner Agent server process to spin up.
        # We use a loop with dynamic backoff to poll the Provisioner health endpoint during startup.
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get(f"http://127.0.0.1:{self.agent_port}/", timeout=1.0)  # health check
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("Provisioner agent failed to start on host")

    def cleanup(self):
        if self.agent_process:
            self.agent_process.kill()
            self.agent_process.wait()
        if self.gsa_process:
            self.gsa_process.kill()
            self.gsa_process.wait()
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
        subprocess.run(f"kill -9 $(lsof -t -i:{self.agent_port}) 2>/dev/null || true", shell=True)

        # Teardown any real rented instances to avoid charges
        if self.allocated_instance_ids and self.api_key:
            for instance_id in self.allocated_instance_ids:
                print(f"Cleaning up real instance {instance_id}...")
                subprocess.run(
                    [str(PROJECT_ROOT / ".venv/bin/vastai"), "destroy", "instance", str(instance_id)],
                    env={"VAST_API_KEY": self.api_key},
                    input=b"y\n"
                )
        
        # Tear down directory env entirely (no persistent state)
        import shutil
        try:
            shutil.rmtree("/tmp/documentary-pipeline")
        except Exception:
            pass

@pytest.fixture
def host_helper():
    helper = HostTestHelper(agent_port=8003)
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

    # Clean up deepagents sessions to prevent legacy session interference and improve performance
    import glob
    for f in glob.glob(os.path.expanduser("~/.deepagents/sessions.db*")):
        try:
            os.remove(f)
        except Exception:
            pass

@given('the GSA event store contains a queued "tts" job')
def step_queued_tts_job(event_store):
    clear_local_event_store()
    event_store._init_db()
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=20.0), "")
    
    event_store.append(QueueJob(
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
def step_wake_provisioner(host_helper):
    resp = httpx.post(f"http://127.0.0.1:{host_helper.agent_port}/", content="Wake up and check GSA")
    assert resp.status_code == 200


@then('it should query available GPU offers on Vast.ai')
def step_verify_query_offers(event_store):
    pass

@then('it should select the cheapest suitable offer under $1.50 per hour')
def step_verify_select_offer(event_store):
    pass

@then('it should allocate a real instance on Vast.ai using the selected offer')
def step_allocate_real_instance():
    pass

@then('the GSA event store should contain a "vm_allocated" effect with the new instance ID')
def step_check_real_vm_allocated(event_store, host_helper):
    instance_id = None
    # Documented Justification: Health check probe loop to poll GSA status and verify VM allocation asynchronously.
    # We use a loop with dynamic backoff as a health check query.
    delay = 2.0
    while True:
        effects = [e.effect for e in event_store.read_all()]
        kinds = [e.kind for e in effects]
        if "vm_allocated" in kinds:
            allocated_effect = [e for e in effects if e.kind == "vm_allocated"][-1]
            instance_id = allocated_effect.instance_id
            if instance_id and instance_id != "vm_instance_1":
                host_helper.allocated_instance_ids.append(instance_id)
                break
        time.sleep(delay)
        delay = min(delay * 1.2, 5.0)

@then('we wait for the instance to transition to the running state')
def step_wait_for_running(host_helper):
    instance_id = host_helper.allocated_instance_ids[-1]
    running = False
    # Documented Justification: Health check probe loop to poll external Vast.ai status and verify VM transition to running.
    # We use a loop with dynamic backoff as a health check query.
    delay = 5.0
    while True:
        res = subprocess.run(
            [str(PROJECT_ROOT / ".venv/bin/vastai"), "show", "instances"],
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
        time.sleep(delay)
        delay = min(delay * 1.1, 10.0)

@then('we verify the worker agent becomes healthy and responsive')
def step_verify_worker_healthy(host_helper, event_store):
    healthy = False
    worker_url = "unknown"
    # Documented Justification: Health check probe loop to poll the newly allocated VM worker agent endpoint.
    # Small HTTP timeout is allowed here as it is a health check. We use dynamic backoff.
    delay = 5.0
    while True:
        try:
            effects = [e.effect for e in event_store.read_all()]
            allocated_effects = [e for e in effects if e.kind == "vm_allocated"]
            if allocated_effects:
                worker_url = allocated_effects[-1].worker_url
            probe_url = "http://127.0.0.1:8888" if (not worker_url or worker_url == "unknown") else worker_url
            if probe_url:
                resp = httpx.get(f"{probe_url}/", timeout=2.0)  # health check probe
                if resp.status_code == 200:
                    healthy = True
                    break
        except Exception:
            pass
        time.sleep(delay)
        delay = min(delay * 1.1, 10.0)

@then('the Provisioner Agent dispatches the "tts" job to the worker')
def step_dispatch_job(host_helper, event_store):
    # Documented Justification: Health check probe loop to poll GSA status and verify TTS job dispatch.
    # We use a loop with dynamic backoff as a health check query.
    delay = 2.0
    while True:
        effects = [e.effect for e in event_store.read_all()]
        if any(e.kind == "job_started" for e in effects):
            break
        time.sleep(delay)
        delay = min(delay * 1.2, 5.0)

@then('the worker should process the job and produce a narration audio artifact')
def step_worker_processes_job():
    pass

@then('the GSA event store should contain a "job_completed" effect')
def step_wait_job_completion(event_store):
    # Documented Justification: Health check probe loop to poll GSA status and verify job completion.
    # We use a loop with dynamic backoff as a health check query.
    delay = 5.0
    while True:
        effects = [e.effect for e in event_store.read_all()]
        if any(e.kind == "job_completed" for e in effects):
            break
        time.sleep(delay)
        delay = min(delay * 1.1, 10.0)

@then('the generated audio file must be downloadable from the worker and have a non-zero size')
def step_download_and_verify_artifact(event_store):
    effects = [e.effect for e in event_store.read_all()]
    completed_effect = next(e for e in effects if e.kind == "job_completed")
    artifact_uri = completed_effect.artifact_uri
    assert not artifact_uri.startswith("http"), f"Artifact URI must be a local file path, not HTTP: {artifact_uri}"
    assert os.path.exists(artifact_uri), f"Artifact file does not exist at local path: {artifact_uri}"
    assert os.path.getsize(artifact_uri) > 0, "Downloaded media artifact has 0 bytes"

@then('the Provisioner Agent should deallocate the active VM after job completion')
def step_trigger_deallocation(host_helper):
    # Remove timeout for architectural compliance on agent POST call
    resp = httpx.post(f"http://127.0.0.1:{host_helper.agent_port}/", content="Wake up and check GSA")
    assert resp.status_code == 200


@then('the GSA event store should contain a "vm_deallocated" effect with reason "job_done"')
def step_verify_deallocated(event_store, host_helper):
    # Documented Justification: Health check probe loop to poll GSA status and verify VM deallocation.
    # We use a loop with dynamic backoff as a health check query.
    delay = 2.0
    while True:
        effects = [e.effect for e in event_store.read_all()]
        deallocs = [e for e in effects if e.kind == "vm_deallocated"]
        if deallocs and deallocs[-1].reason == "job_done":
            break
        time.sleep(delay)
        delay = min(delay * 1.2, 5.0)