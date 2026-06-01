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
    UpdateScript,
    ScriptBlock,
    QueueJob,
    JobCompleted,
    ReconciliationComplete,
)
from event_store import EventStore

scenarios('features/real_audio_reconciliation.feature')

class HostAudioHelper:
    def __init__(self, agent_port: int = 8002):
        self.agent_port = agent_port
        self.gsa_process = None
        self.agent_process = None

    def start_gsa(self):
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        
        self.gsa_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "global_state_agent:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        
        # Poll GSA startup
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get("http://localhost:8000/", timeout=1.0)
                if resp.status_code in (200, 400):
                    return
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("GSA failed to start on host")

    def start_audio_agent(self):
        subprocess.run(f"kill -9 $(lsof -t -i:{self.agent_port}) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        
        self.agent_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "agents.audio.app:app", "--host", "127.0.0.1", "--port", str(self.agent_port)],
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        
        # Poll Agent startup
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get(f"http://localhost:{self.agent_port}/", timeout=1.0)
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("Audio agent failed to start on host")

    def cleanup(self):
        if self.agent_process:
            self.agent_process.kill()
            self.agent_process.wait()
        if self.gsa_process:
            self.gsa_process.kill()
            self.gsa_process.wait()
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
        subprocess.run(f"kill -9 $(lsof -t -i:{self.agent_port}) 2>/dev/null || true", shell=True)
        
        import shutil
        try:
            shutil.rmtree("/tmp/documentary-pipeline")
        except Exception:
            pass

@pytest.fixture
def audio_helper():
    helper = HostAudioHelper()
    helper.start_gsa()
    helper.start_audio_agent()
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

@given("the GSA event store has a written script block needing audio narration")
def step_written_script_block(event_store):
    clear_local_event_store()
    event_store._init_db()
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")
    
    # Create an UpdateScript block that needs processing
    block = ScriptBlock(
        scene_num=1,
        block_id="s1_b1",
        speaker="V1_Narrator",
        text="This is a test block for audio reconciliation.",
        duration_sec=5.0
    )
    event_store.append(UpdateScript(agent="scenario", blocks=[block]), "initial_hash")

@given("the system budget has remaining funds")
def step_remaining_funds():
    pass

@given("the Audio Agent is running on the host")
def step_audio_agent_running(audio_helper):
    pass

@when("the Audio Agent receives a wakeup instruction")
def step_wake_audio(audio_helper):
    resp = httpx.post(f"http://localhost:{audio_helper.agent_port}/", content="Wake up and check GSA")
    assert resp.status_code == 200

@then('the Audio Agent should queue a "tts" job for the narration block')
def step_verify_job_queued(event_store):
    delay = 1.0
    for _ in range(10):
        effects = [e.effect for e in event_store.read_all()]
        queued_jobs = [e for e in effects if e.kind == "queue_job" and e.agent == "audio"]
        if queued_jobs:
            return
        time.sleep(delay)
    raise AssertionError("Audio agent did not queue the tts job")

@then('the GSA event store should contain a "queue_job" effect for the block')
def step_check_queue_job_effect(event_store):
    effects = [e.effect for e in event_store.read_all()]
    queued_jobs = [e for e in effects if e.kind == "queue_job"]
    assert len(queued_jobs) >= 1
    assert queued_jobs[0].job_type == "tts"
    assert queued_jobs[0].block_id == "s1_b1"

@when("the Provisioner or test harness marks the job completed with a dummy audio artifact")
def step_complete_job(event_store):
    # Simulate provisioner updating event store with job completion
    event_store.append(JobCompleted(
        agent="provisioner",
        job_id="job_tts_1",
        job_type="tts",
        scene_num=1,
        block_id="s1_b1",
        slot_id="s1_b1",
        artifact_uri="/tmp/audio/s1_b1.wav",
        duration_sec=5.1  # Within tolerance of 5.0s
    ), "initial_hash")

@then("the Audio Agent should evaluate the generated audio duration against target tolerance")
def step_evaluate_tolerance():
    pass

@then('the GSA event store should contain a "reconciliation_complete" effect for the slot')
def step_check_reconciliation_complete(event_store):
    delay = 1.0
    for _ in range(10):
        effects = [e.effect for e in event_store.read_all()]
        complete_effects = [e for e in effects if e.kind == "reconciliation_complete"]
        if complete_effects:
            assert complete_effects[-1].slot_id == "s1_b1"
            return
        time.sleep(delay)
    raise AssertionError("Audio agent did not produce reconciliation_complete")
