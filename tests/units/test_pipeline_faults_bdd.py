import os
import sys
import time
import pytest
import httpx
from pathlib import Path
from pytest_bdd import scenarios, given, when, then, parsers

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from effects import (
    PipelineStarted,
    BudgetSet,
    UpdateScript,
    ScriptBlock,
    QueueJob,
    JobCompleted,
    VMAllocated,
    JobStarted,
)
from event_store import EventStore

# Load scenarios
scenarios('features/pipeline_faults.feature')


class MultiAgentTestHelper:
    def __init__(self):
        self.gsa_process = None

    def start_gsa(self):
        import subprocess
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        env["PYTHONUNBUFFERED"] = "1"

        # Read api key
        api_key = ""
        _deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
        if os.path.exists(_deepseek_key_path):
            with open(_deepseek_key_path) as f:
                api_key = f.read().strip()
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key

        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.gsa_stdout = open(log_dir / "gsa_stdout_bdd.log", "w")
        self.gsa_stderr = open(log_dir / "gsa_stderr_bdd.log", "w")

        self.gsa_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "global_state_agent:app", "--host", "127.0.0.1", "--port", "8000"],
            stdout=self.gsa_stdout,
            stderr=self.gsa_stderr,
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        
        # Poll GSA
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get("http://127.0.0.1:8000/", headers={"accept": "application/json"}, timeout=1.0)
                if resp.status_code in (200, 400):
                    return
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("GSA failed to start")

    def cleanup(self):
        if self.gsa_process:
            try:
                self.gsa_process.kill()
                self.gsa_process.wait()
            except Exception:
                pass
            try:
                self.gsa_stdout.close()
                self.gsa_stderr.close()
            except Exception:
                pass
        import subprocess
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)


@pytest.fixture
def bdd_orchestration_helper():
    helper = MultiAgentTestHelper()
    helper.start_gsa()
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


# ===========================================================================
# Step Definitions
# ===========================================================================

@given("a clean local pipeline database")
def step_clean_db(event_store):
    clear_local_event_store()
    event_store._init_db()
    with event_store._connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
    
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=20.0), "")


@when("the Provisioner allocates multiple VMs with separate worker URLs")
def step_allocate_vms(event_store):
    v1 = VMAllocated(
        agent="provisioner",
        instance_id="39190805",
        role="tts",
        offer_id="offer_1",
        worker_url="http://127.0.0.1:8888",
        gpu_type="RTX 4090",
        cost_per_hour=0.73,
    )
    v2 = VMAllocated(
        agent="provisioner",
        instance_id="39178262",
        role="ltx",
        offer_id="offer_2",
        worker_url="http://127.0.0.1:8889",
        gpu_type="RTX 4090",
        cost_per_hour=0.76,
    )
    event_store.append(v1, "")
    event_store.append(v2, "")


@then("all active VMs must have unique non-empty worker URLs in GSA")
def step_check_vm_conformance(bdd_orchestration_helper):
    # Wait for GSA to sync
    time.sleep(1.0)
    
    # Query GSA via HTTP to check JSON projections
    resp = httpx.get("http://127.0.0.1:8000/", headers={"accept": "application/json"})
    assert resp.status_code == 200
    state = resp.json()
    
    vms_list = list(state["vms"]["vms"].values())
    assert len(vms_list) == 2
    
    urls = [vm["worker_url"] for vm in vms_list if vm["worker_url"]]
    assert len(urls) == len(set(urls)), f"Duplicate worker URLs: {urls}"
    
    for vm in vms_list:
        assert vm["worker_url"] != ""
        assert vm["worker_url"] != "unknown"


@when("a TTS job is queued, completed, and then a new job is queued for the same block")
def step_job_cycle(event_store):
    # 1. Queue first job
    q1 = QueueJob(
        agent="audio",
        job_id="job_1",
        job_type="tts",
        scene_num=1,
        block_id="s1_b1",
        slot_id="A1:1:s1_b1",
        params={"text": "Hello"},
    )
    event_store.append(q1, "")
    
    # 2. Complete first job
    c1 = JobCompleted(
        agent="provisioner",
        job_id="job_1",
        artifact_uri="/tmp/audio1.wav",
        duration_sec=3.0,
        vm_instance_id="39190805",
    )
    event_store.append(c1, "")

    # 3. Queue second job
    q2 = QueueJob(
        agent="audio",
        job_id="job_2",
        job_type="tts",
        scene_num=1,
        block_id="s1_b1",
        slot_id="A1:1:s1_b1",
        params={"text": "Hello Revised"},
    )
    event_store.append(q2, "")


@then("GSA must correctly mark the block as dirty while the new job is pending")
def step_check_dirty_state(bdd_orchestration_helper):
    # Wait for GSA to sync
    time.sleep(1.0)
    
    # Query GSA via HTTP to check JSON projections
    resp = httpx.get("http://127.0.0.1:8000/", headers={"accept": "application/json"})
    assert resp.status_code == 200
    state = resp.json()
    
    dirty_blocks = state["jobs"]["dirty_blocks"]
    clean_blocks = state["jobs"]["clean_blocks"]
    
    assert "A1:1:s1_b1" in dirty_blocks
    assert "A1:1:s1_b1" not in clean_blocks
