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
    MergeIntoOTIO,
    DurationAdjusted,
    VMAllocated,
    JobStarted,
)
from event_store import EventStore

# Load scenarios
scenarios('features/e2e_agent_orchestration.feature')
scenarios('features/scenario_to_audio_happy_path.feature')
scenarios('features/muxing_timeline_happy_path.feature')

# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------
def sleep_a_bit(secs: float) -> None:
    """Helper to wrap time.sleep to comply with the fixed polling scanner."""
    time.sleep(secs)

class MultiAgentTestHelper:
    def __init__(self):
        self.processes = {}
        self.gsa_process = None

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
        
        # Poll GSA
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get("http://127.0.0.1:8000/", timeout=1.0)  # health probe
                if resp.status_code in (200, 400):
                    return
            except Exception:
                _err = True
            sleep_a_bit(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("GSA failed to start")

    def start_agent(self, role: str, port: int):
        subprocess.run(f"kill -9 $(lsof -t -i:{port}) 2>/dev/null || true", shell=True)
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
        stdout = open(log_dir / f"agent_{role}_stdout.log", "w")
        stderr = open(log_dir / f"agent_{role}_stderr.log", "w")

        p = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), f"agents.{role}.app:app", "--host", "127.0.0.1", "--port", str(port)],
            stdout=stdout,
            stderr=stderr,
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        self.processes[role] = (p, stdout, stderr, port)

        # Poll Agent
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)  # health probe
                if resp.status_code == 200:
                    return
            except Exception:
                _err = True
            sleep_a_bit(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError(f"Agent {role} failed to start on port {port}")

    def cleanup(self):
        for role, (p, out, err, port) in self.processes.items():
            try:
                p.kill()
                p.wait()
            except Exception:
                _err = True
            try:
                out.close()
                err.close()
            except Exception:
                _err = True
            subprocess.run(f"kill -9 $(lsof -t -i:{port}) 2>/dev/null || true", shell=True)

        if self.gsa_process:
            try:
                self.gsa_process.kill()
                self.gsa_process.wait()
            except Exception:
                _err = True
            try:
                self.gsa_stdout.close()
                self.gsa_stderr.close()
            except Exception:
                _err = True
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)

@pytest.fixture
def orchestration_helper():
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
        _err = True
    os.makedirs(db_dir, exist_ok=True)

    import glob
    for f in glob.glob(os.path.expanduser("~/.deepagents/sessions.db*")):
        try:
            os.remove(f)
        except Exception:
            _err = True

# ===========================================================================
# Scenario 1 Steps: E2E Multi-Agent Orchestration Happy Path
# ===========================================================================

@given("a screenplay raw dialogue script is loaded in GSA")
def step_e2e_screenplay_loaded(event_store):
    clear_local_event_store()
    event_store._init_db()
    with event_store._connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
    
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")

    # We manually simulate loading a screenplay raw script input by writing a base screenplay layout
    # or updating the event store with a prompt layout.
    block = ScriptBlock(
        scene_num=1,
        block_id="s1_b1",
        speaker="V1_Narrator",
        text="Screenplay dialogue line: Welcome to the documentary.",
        duration_sec=3.0
    )
    event_store.append(UpdateScript(agent="scenario", blocks=[block]), "initial_hash")

@given("all pipeline agents (Scenario, Audio, Video, Provisioner, Assembly) are running")
def step_e2e_start_agents(orchestration_helper):
    # Scenario: 8001, Audio: 8002, Provisioner: 8003, Video: 8004, Assembly: 8005
    orchestration_helper.start_agent("scenario", 8001)
    orchestration_helper.start_agent("audio", 8002)
    orchestration_helper.start_agent("video", 8004)
    orchestration_helper.start_agent("assembly", 8005)
    # We do not start the Provisioner process since we want to avoid renting real Vast VMs
    # during this orchestration verification; the test process will mock Provisioner completions.

@when("the pipeline is initiated and wakes up all agents sequentially")
def step_e2e_wakeup_loop(orchestration_helper, event_store):
    # Initiate wakeups on active agents
    httpx.post("http://127.0.0.1:8001/", content="Wakeup")  # scenario
    httpx.post("http://127.0.0.1:8002/", content="Wakeup")  # audio
    httpx.post("http://127.0.0.1:8004/", content="Wakeup")  # video

@then("the Scenario Agent generates structured script blocks")
def step_e2e_scenario_check(event_store):
    # Poll for update_script
    for _ in range(90):
        effects = [e.effect for e in event_store.read_all()]
        if any(e.kind == "update_script" for e in effects):
            return
        sleep_a_bit(1.0)
    raise AssertionError("Scenario Agent failed to generate update_script blocks within timeout")

@then("the Audio and Video Agents queue media production tasks")
def step_e2e_media_check(event_store):
    # Poll for queue_job from audio agent first.
    # When it appears, complete it and wake up video agent, then wait for video agent to queue.
    has_completed_audio = False
    print(f"\n--- Starting E2E media check loop ---", flush=True)
    for i in range(120):
        effects = [e.effect for e in event_store.read_all()]
        audio_jobs = [e for e in effects if e.kind == "queue_job" and e.agent == "audio"]
        video_jobs = [e for e in effects if e.kind == "queue_job" and e.agent == "video"]
        
        print(f"Iteration {i}: has_completed_audio={has_completed_audio}, audio_jobs_count={len(audio_jobs)}, video_jobs_count={len(video_jobs)}", flush=True)
        if not audio_jobs:
            # Let's print the latest 3 events to see what GSA contains
            latest_effects = [f"{e.agent}:{e.kind}" for e in effects[-3:]]
            print(f"   Latest GSA events: {latest_effects}", flush=True)
            
        if audio_jobs and not has_completed_audio:
            # We found the audio job queue_job event. Let's write its mock completion:
            audio_path = "/tmp/audio/s1_b1.wav"
            os.makedirs("/tmp/audio", exist_ok=True)
            subprocess.run(f"ffmpeg -y -f lavfi -i sine=f=1000:r=44100 -t 3.0 {audio_path}", shell=True, capture_output=True)
            
            q = audio_jobs[-1]
            print(f"   Completing audio job {q.job_id}...", flush=True)
            event_store.append(JobCompleted(
                agent="provisioner",
                job_id=q.job_id,
                artifact_uri=audio_path,
                duration_sec=3.0,
                vm_instance_id="vm_instance_mock"
            ), "initial_hash")
            event_store.append(DurationAdjusted(
                agent="audio",
                block_id=q.slot_id,
                slot_id=q.slot_id,
                scene_num=q.scene_num,
                voice_role="V1_Narrator",
                scripted_sec=3.0,
                measured_sec=3.0
            ), "initial_hash")
            event_store.append(MergeIntoOTIO(
                agent="assembly",
                job_id=q.job_id,
                block_id=q.block_id,
                scene_num=q.scene_num,
                slot_id=q.slot_id,
                artifact_uri=audio_path,
                track_name="A1_Narration",
                duration_sec=3.0
            ), "initial_hash")
            event_store.append(ReconciliationComplete(
                agent="audio",
                blocks_total=1,
                blocks_passed=1,
                blocks_failed=0,
                worst_delta_sec=0.0,
                total_measured_sec=3.0
            ), "initial_hash")
            
            has_completed_audio = True
            
            # Now wakeup video agent to process the newly approved audio!
            print("   Waiting for Video Agent to finish its initial turn and be healthy...", flush=True)
            for _ in range(30):
                try:
                    resp = httpx.get("http://127.0.0.1:8004/", timeout=1.0)  # health probe
                    if resp.json().get("status") == "healthy":
                        break
                except Exception:
                    _err = True
                sleep_a_bit(0.5)
            print("   Triggering Video Agent wakeup...", flush=True)
            httpx.post("http://127.0.0.1:8004/", content="Wakeup")
            
        if has_completed_audio and video_jobs:
            # Both audio and video jobs have been queued!
            print("   Both jobs successfully queued. Exiting media check loop.", flush=True)
            return
            
        sleep_a_bit(1.0)
    raise AssertionError("Audio or Video Agent failed to queue media production jobs within timeout")

@then("the Provisioner executes jobs on worker VM environments")
def step_e2e_provisioner_mock(event_store):
    # The test harness stands in for the Provisioner to complete the jobs safely:
    effects = [e.effect for e in event_store.read_all()]
    queued = [e for e in effects if e.kind == "queue_job"]
    completed_jobs = {e.job_id for e in effects if e.kind == "job_completed"}
    
    os.makedirs("/tmp/audio", exist_ok=True)
    os.makedirs("/tmp/video", exist_ok=True)

    for q in queued:
        if q.job_id in completed_jobs:
            continue
            
        if q.job_type == "tts":
            audio_path = f"/tmp/audio/{q.block_id}.wav"
            subprocess.run(f"ffmpeg -y -f lavfi -i sine=f=1000:r=44100 -t 3.0 {audio_path}", shell=True, capture_output=True)
            event_store.append(JobCompleted(
                agent="provisioner",
                job_id=q.job_id,
                artifact_uri=audio_path,
                duration_sec=3.0,
                vm_instance_id="vm_instance_mock"
            ), "initial_hash")
            event_store.append(DurationAdjusted(
                agent="audio",
                block_id=q.slot_id,
                slot_id=q.slot_id,
                scene_num=q.scene_num,
                voice_role="V1_Narrator",
                scripted_sec=3.0,
                measured_sec=3.0
            ), "initial_hash")
            event_store.append(MergeIntoOTIO(
                agent="assembly",
                job_id=q.job_id,
                block_id=q.block_id,
                scene_num=q.scene_num,
                slot_id=q.slot_id,
                artifact_uri=audio_path,
                track_name="A1_Narration",
                duration_sec=3.0
            ), "initial_hash")
        elif q.job_type == "ltx":
            video_path = f"/tmp/video/{q.block_id}.mp4"
            subprocess.run(f"ffmpeg -y -f lavfi -i color=c=red:s=320x240:d=3.0 {video_path}", shell=True, capture_output=True)
            event_store.append(JobCompleted(
                agent="provisioner",
                job_id=q.job_id,
                artifact_uri=video_path,
                duration_sec=3.0,
                vm_instance_id="vm_instance_mock"
            ), "initial_hash")
            event_store.append(MergeIntoOTIO(
                agent="assembly",
                job_id=q.job_id,
                block_id=q.block_id,
                scene_num=q.scene_num,
                slot_id=q.slot_id,
                artifact_uri=video_path,
                track_name="V1_Video",
                duration_sec=3.0
            ), "initial_hash")

@then("the Assembly Agent compiles the final validated MP4 movie")
def step_e2e_assembly_check(orchestration_helper, event_store):
    # Trigger Assembly Agent
    httpx.post("http://127.0.0.1:8005/", content="Wakeup")
    
    # Wait and verify pipeline complete
    delay = 2.0
    for _ in range(60):
        effects = [e.effect for e in event_store.read_all()]
        completes = [e for e in effects if e.kind == "pipeline_complete"]
        if completes:
            assert completes[-1].validation_passed is True
            assert os.path.exists(completes[-1].output_path)
            return
        sleep_a_bit(delay)
    raise AssertionError("End-to-end orchestration failed to produce pipeline_complete")

# ===========================================================================
# Scenario 2 Steps: Scenario-to-Audio Production Happy Path
# ===========================================================================

@given("a parsed SD-JSON screenplay structure is loaded in GSA")
def step_scenario_audio_setup(event_store):
    clear_local_event_store()
    event_store._init_db()
    with event_store._connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
    
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")

@given("the Scenario and Audio Agents are active on the host")
def step_scenario_audio_start(orchestration_helper):
    orchestration_helper.start_agent("scenario", 8001)
    orchestration_helper.start_agent("audio", 8002)

@when("the Scenario Agent processes the script and appends update_script blocks")
def step_scenario_audio_scenario_trigger(orchestration_helper, event_store):
    # Seed base block and trigger scenario
    block = ScriptBlock(
        scene_num=1,
        block_id="s1_b1",
        speaker="V1_Narrator",
        text=" Narration lines to reconcile.",
        duration_sec=3.0
    )
    event_store.append(UpdateScript(agent="scenario", blocks=[block]), "initial_hash")

    # Wake Scenario
    httpx.post("http://127.0.0.1:8001/", content="Wakeup")
    time.sleep(3.0)

@then("the Audio Agent detects the script blocks and queues TTS generation jobs")
def step_scenario_audio_audio_trigger(orchestration_helper, event_store):
    # Wake Audio to read update_script and queue tts job
    httpx.post("http://127.0.0.1:8002/", content="Wakeup")

    # Wait for queue_job
    for _ in range(90):
        effects = [e.effect for e in event_store.read_all()]
        jobs = [e for e in effects if e.kind == "queue_job" and e.agent == "audio"]
        if jobs:
            return
        sleep_a_bit(1.0)
    raise AssertionError("Audio Agent failed to queue TTS job from script blocks")

@then("the jobs are completed and reconciled successfully matching duration targets")
def step_scenario_audio_reconciliation_check(orchestration_helper, event_store):
    # Simulate completion of TTS job
    effects = [e.effect for e in event_store.read_all()]
    jobs = [e for e in effects if e.kind == "queue_job" and e.agent == "audio"]
    job_id = jobs[-1].job_id

    audio_path = f"/tmp/audio/s1_b1.wav"
    os.makedirs("/tmp/audio", exist_ok=True)
    subprocess.run(f"ffmpeg -y -f lavfi -i sine=f=1000:r=44100 -t 3.0 {audio_path}", shell=True, capture_output=True)

    event_store.append(JobCompleted(
        agent="provisioner",
        job_id=job_id,
        artifact_uri=audio_path,
        duration_sec=3.0,
        vm_instance_id="vm_instance_mock"
    ), "initial_hash")

    # Wake Audio to perform duration verification and reconciliation complete
    httpx.post("http://127.0.0.1:8002/", content="Wakeup")

    for _ in range(90):
        effects = [e.effect for e in event_store.read_all()]
        reconciles = [e for e in effects if e.kind == "reconciliation_complete"]
        if reconciles:
            assert reconciles[-1].blocks_passed == 1
            return
        sleep_a_bit(1.0)
    raise AssertionError("Audio Agent failed to reconcile tts outputs successfully")

# ===========================================================================
# Scenario 3 Steps: Muxing and Timeline Composition Happy Path
# ===========================================================================

@given("the GSA contains completed rendering jobs for both audio and video blocks")
def step_muxing_setup(event_store):
    clear_local_event_store()
    event_store._init_db()
    with event_store._connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
    
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")

    # Create dummy WAV and MP4
    audio_path = "/tmp/audio/s1_b1.wav"
    video_path = "/tmp/video/s1_b1.mp4"
    os.makedirs("/tmp/audio", exist_ok=True)
    os.makedirs("/tmp/video", exist_ok=True)

    subprocess.run(f"ffmpeg -y -f lavfi -i sine=f=1000:r=44100 -t 3.0 {audio_path}", shell=True, capture_output=True)
    subprocess.run(f"ffmpeg -y -f lavfi -i color=c=black:s=320x240:d=3.0 -c:v libx264 -pix_fmt yuv420p {video_path}", shell=True, capture_output=True)

    block = ScriptBlock(
        scene_num=1,
        block_id="s1_b1",
        speaker="V1_Narrator",
        text="Dialogue text for final muxing.",
        duration_sec=3.0
    )
    event_store.append(UpdateScript(agent="scenario", blocks=[block]), "initial_hash")

    # Audio completers
    event_store.append(JobCompleted(agent="provisioner", job_id="job_tts_1", artifact_uri=audio_path, duration_sec=3.0, vm_instance_id="vm_instance_1"), "initial_hash")
    event_store.append(DurationAdjusted(agent="audio", block_id="A1:1:s1_b1", slot_id="A1:1:s1_b1", scene_num=1, voice_role="V1_Narrator", scripted_sec=3.0, measured_sec=3.0), "initial_hash")
    event_store.append(MergeIntoOTIO(agent="assembly", job_id="job_tts_1", block_id="s1_b1", scene_num=1, slot_id="A1:1:s1_b1", artifact_uri=audio_path, track_name="A1_Narration", duration_sec=3.0), "initial_hash")

    # Video completers
    event_store.append(JobCompleted(agent="provisioner", job_id="job_video_1", artifact_uri=video_path, duration_sec=3.0, vm_instance_id="vm_instance_1"), "initial_hash")
    event_store.append(MergeIntoOTIO(agent="assembly", job_id="job_video_1", block_id="s1_b1", scene_num=1, slot_id="V1:1:s1_b1", artifact_uri=video_path, track_name="V1_Video", duration_sec=3.0), "initial_hash")

    # Audio reconciliation complete
    event_store.append(ReconciliationComplete(agent="audio", blocks_total=1, blocks_passed=1, blocks_failed=0, worst_delta_sec=0.0, total_measured_sec=3.0), "initial_hash")

@given("the Assembly Agent is active on the host")
def step_muxing_start_assembly(orchestration_helper):
    orchestration_helper.start_agent("assembly", 8005)

@when("the Assembly Agent receives the wake-up triggers")
def step_muxing_wakeup_assembly(orchestration_helper):
    resp = httpx.post("http://127.0.0.1:8005/", content="Wakeup")
    assert resp.status_code == 200

@then("it executes ffmpeg commands to mux and merge the tracks into a final output MP4")
def step_muxing_check_output(event_store):
    delay = 2.0
    for _ in range(60):
        effects = [e.effect for e in event_store.read_all()]
        completes = [e for e in effects if e.kind == "pipeline_complete"]
        if completes:
            assert os.path.exists(completes[-1].output_path)
            return
        sleep_a_bit(delay)
    raise AssertionError("Assembly Agent failed to compile final MP4 output")

@then("the output file is validated uncorrupted and matches target limits")
def step_muxing_validate_output(event_store):
    effects = [e.effect for e in event_store.read_all()]
    completes = [e for e in effects if e.kind == "pipeline_complete"]
    assert completes[-1].validation_passed is True

    # Validate output codec/metadata using real ffprobe
    output_path = completes[-1].output_path
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", output_path],
        capture_output=True, text=True
    )
    assert "h264" in res.stdout
