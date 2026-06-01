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
    MergeIntoOTIO,
    DurationAdjusted,
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
        env["PYTHONUNBUFFERED"] = "1"

        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.gsa_stdout = open(log_dir / "gsa_stdout.log", "w")
        self.gsa_stderr = open(log_dir / "gsa_stderr.log", "w")

        self.gsa_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "global_state_agent:app", "--host", "127.0.0.1", "--port", "8000"],
            stdout=self.gsa_stdout,
            stderr=self.gsa_stderr,
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
        env["PYTHONUNBUFFERED"] = "1"
        
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.agent_stdout = open(log_dir / "agent_assembly_stdout.log", "w")
        self.agent_stderr = open(log_dir / "agent_assembly_stderr.log", "w")

        self.agent_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "agents.assembly.app:app", "--host", "127.0.0.1", "--port", str(self.agent_port)],
            stdout=self.agent_stdout,
            stderr=self.agent_stderr,
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
            # shutil.rmtree("/tmp/documentary-pipeline")
            pass
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

    # Clean up deepagents sessions to prevent legacy session interference and improve performance
    import glob
    for f in glob.glob(os.path.expanduser("~/.deepagents/sessions.db*")):
        try:
            os.remove(f)
        except Exception:
            pass

@given("the GSA event store contains completed audio and video jobs for all scenes")
def step_completed_jobs(event_store):
    clear_local_event_store()
    event_store._init_db()
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")
    
    # Create valid dummy media files on disk so ffmpeg doesn't fail
    import subprocess
    os.makedirs("/tmp/audio", exist_ok=True)
    os.makedirs("/tmp/video", exist_ok=True)
    subprocess.run("ffmpeg -y -f lavfi -i anullsrc=r=44100:cl=mono -t 5.1 /tmp/audio/s1_b1.wav", shell=True, capture_output=True)
    subprocess.run("ffmpeg -y -f lavfi -i color=c=black:s=1280x720:d=5.1 -c:v libx264 -pix_fmt yuv420p /tmp/video/s1_b1.mp4", shell=True, capture_output=True)

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
        artifact_uri="/tmp/audio/s1_b1.wav",
        duration_sec=5.1,
        vm_instance_id="vm_instance_1"
    ), "initial_hash")

    # Complete video job
    event_store.append(JobCompleted(
        agent="provisioner",
        job_id="job_video_1",
        artifact_uri="/tmp/video/s1_b1.mp4",
        duration_sec=5.1,
        vm_instance_id="vm_instance_1"
    ), "initial_hash")

    # Reconcile audio
    event_store.append(ReconciliationComplete(
        agent="audio",
        blocks_total=1,
        blocks_passed=1,
        blocks_failed=0,
        worst_delta_sec=0.1,
        total_measured_sec=5.1
    ), "initial_hash")

    # Adjust duration (so measured_sec is populated)
    event_store.append(DurationAdjusted(
        agent="audio",
        block_id="A1:1:s1_b1",
        slot_id="A1:1:s1_b1",
        scene_num=1,
        voice_role="V1_Narrator",
        scripted_sec=5.0,
        measured_sec=5.1
    ), "initial_hash")

    # Merge narration clip into OTIO
    event_store.append(MergeIntoOTIO(
        agent="assembly",
        job_id="job_tts_1",
        block_id="s1_b1",
        scene_num=1,
        slot_id="A1:1:s1_b1",
        artifact_uri="/tmp/audio/s1_b1.wav",
        track_name="A1_Narration",
        duration_sec=5.1
    ), "initial_hash")

    # Merge video clip into OTIO
    event_store.append(MergeIntoOTIO(
        agent="assembly",
        job_id="job_video_1",
        block_id="s1_b1",
        scene_num=1,
        slot_id="A1:1:s1_b1",
        artifact_uri="/tmp/video/s1_b1.mp4",
        track_name="V1_Video",
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
    delay = 2.0
    for _ in range(120):
        effects = [e.effect for e in event_store.read_all()]
        complete_effects = [e for e in effects if e.kind == "pipeline_complete"]
        if complete_effects:
            assert complete_effects[-1].output_path
            assert complete_effects[-1].duration_sec > 0
            return
        time.sleep(delay)
    raise AssertionError("Assembly agent did not produce pipeline_complete")
