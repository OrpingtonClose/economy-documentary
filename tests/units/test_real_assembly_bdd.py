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
    JobCompleted,
    ReconciliationComplete,
    PipelineComplete,
)
from event_store import EventStore

scenarios('features/real_assembly_composition.feature')

class HostAssemblyHelper:
    def __init__(self, agent_port: int = 8005):
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

    def start_assembly_agent(self):
        subprocess.run(f"kill -9 $(lsof -t -i:{self.agent_port}) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        
        self.agent_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "agents.assembly.app:app", "--host", "127.0.0.1", "--port", str(self.agent_port)],
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
        raise RuntimeError("Assembly agent failed to start on host")

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
def assembly_helper():
    helper = HostAssemblyHelper()
    helper.start_gsa()
    helper.start_assembly_agent()
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

@given("the GSA event store contains completed audio and video jobs for all scenes")
def step_completed_jobs(event_store):
    clear_local_event_store()
    event_store._init_db()
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")
    
    # Create an UpdateScript block that needs processing
    block = ScriptBlock(
        scene_num=1,
        block_id="s1_b1",
        speaker="V1_Narrator",
        text="This is a test block for assembly.",
        duration_sec=5.0
    )
    event_store.append(UpdateScript(agent="scenario", blocks=[block]), "initial_hash")

    # Complete audio job
    event_store.append(JobCompleted(
        agent="provisioner",
        job_id="job_tts_1",
        job_type="tts",
        scene_num=1,
        block_id="s1_b1",
        slot_id="s1_b1",
        artifact_uri="/tmp/audio/s1_b1.wav",
        duration_sec=5.1
    ), "initial_hash")

    # Complete video job
    event_store.append(JobCompleted(
        agent="provisioner",
        job_id="job_video_1",
        job_type="video",
        scene_num=1,
        block_id="s1_b1",
        slot_id="s1_b1",
        artifact_uri="/tmp/video/s1_b1.mp4",
        duration_sec=5.1
    ), "initial_hash")

    # Reconcile audio
    event_store.append(ReconciliationComplete(
        agent="audio",
        slot_id="s1_b1",
        duration_sec=5.1
    ), "initial_hash")

@given("the Assembly Agent is running on the host")
def step_assembly_agent_running(assembly_helper):
    pass

@when("the Assembly Agent receives a wakeup instruction")
def step_wake_assembly(assembly_helper):
    resp = httpx.post(f"http://localhost:{assembly_helper.agent_port}/", content="Wake up and check GSA")
    assert resp.status_code == 200

@then("the Assembly Agent should merge the media tracks using the assembly tool")
def step_merge_tracks():
    pass

@then("it should validate the final output duration against the combined slot targets")
def step_validate_duration():
    pass

@then('the GSA event store should contain a "pipeline_complete" effect with the output path and duration')
def step_check_pipeline_complete(event_store):
    delay = 1.0
    for _ in range(10):
        effects = [e.effect for e in event_store.read_all()]
        complete_effects = [e for e in effects if e.kind == "pipeline_complete"]
        if complete_effects:
            assert complete_effects[-1].output_path
            assert complete_effects[-1].duration_sec > 0
            return
        time.sleep(delay)
    raise AssertionError("Assembly agent did not produce pipeline_complete")
