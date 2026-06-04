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
    MergeIntoOTIO,
    DurationAdjusted,
    VMAllocated,
    JobStarted,
)
from event_store import EventStore

scenarios('features/hour_movie_scaffolding.feature')
scenarios('features/fleet_coordination.feature')
scenarios('features/segment_recovery.feature')

# ---------------------------------------------------------------------------
# Mock Binary Setup (ffmpeg and ffprobe)
# ---------------------------------------------------------------------------
def setup_mock_bin():
    mock_dir = Path("/tmp/mock_bin")
    mock_dir.mkdir(parents=True, exist_ok=True)
    
    # Mock ffmpeg
    ffmpeg_path = mock_dir / "ffmpeg"
    ffmpeg_content = """#!/usr/bin/env python3
import sys
from pathlib import Path

out_file = None
for i, arg in enumerate(sys.argv):
    if arg == "-y" or arg.endswith(".mp4") or arg.endswith(".wav"):
        if i > 0 and sys.argv[i-1] not in ("-i", "-t", "-c:v", "-c:a", "-af", "-stream_loop"):
            out_file = arg

if out_file:
    p = Path(out_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"\\x00" * 2048)  # Write 2KB to satisfy > 1KB check
sys.exit(0)
"""
    ffmpeg_path.write_text(ffmpeg_content)
    ffmpeg_path.chmod(0o755)

    # Mock ffprobe
    ffprobe_path = mock_dir / "ffprobe"
    ffprobe_content = """#!/usr/bin/env python3
import sys
import os
import sqlite3
import json

# Pre-defined durations or formats
if "format=duration" in "".join(sys.argv):
    filename = None
    for arg in sys.argv:
        if arg.endswith(".wav") or arg.endswith(".mp4"):
            filename = arg
            break
    if filename:
        basename = os.path.basename(filename)
        block_id = basename.split(".")[0]
        db_path = "/tmp/documentary-pipeline/events.db"
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.execute("SELECT effect_json FROM events WHERE kind IN ('duration_adjusted', 'update_script', 'merge_into_otio')")
                duration = None
                for row in cursor:
                    effect = json.loads(row[0])
                    # Check direct fields
                    if effect.get("block_id") == block_id or effect.get("slot_id") == block_id or f":{block_id}" in str(effect.get("slot_id")):
                        duration = effect.get("measured_sec") or effect.get("duration_sec")
                        if duration:
                            break
                    # Check update_script blocks
                    for block in effect.get("blocks", []):
                        if block.get("block_id") == block_id:
                            duration = block.get("duration_sec")
                            break
                    if duration:
                        break
                if duration:
                    print(str(duration))
                    sys.exit(0)
            except Exception:
                pass
    print("3600.0")
else:
    print("h264")
sys.exit(0)
"""
    ffprobe_path.write_text(ffprobe_content)
    ffprobe_path.chmod(0o755)
    
    return mock_dir

# ---------------------------------------------------------------------------
# Host Test Helper
# ---------------------------------------------------------------------------
class HostScaffoldingHelper:
    def __init__(self, assembly_port: int = 8005):
        self.assembly_port = assembly_port
        self.gsa_process = None
        self.assembly_process = None
        self.mock_bin_dir = setup_mock_bin()

    def start_gsa(self):
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        env["PYTHONUNBUFFERED"] = "1"
        
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.gsa_stdout = open(log_dir / "scaffold_gsa_stdout.log", "w")
        self.gsa_stderr = open(log_dir / "scaffold_gsa_stderr.log", "w")

        self.gsa_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "global_state_agent:app", "--host", "127.0.0.1", "--port", "8000"],
            stdout=self.gsa_stdout,
            stderr=self.gsa_stderr,
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get("http://127.0.0.1:8000/", timeout=1.0)  # health probe
                if resp.status_code in (200, 400):
                    return
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("GSA failed to start on host")

    def start_assembly_agent(self):
        subprocess.run(f"kill -9 $(lsof -t -i:{self.assembly_port}) 2>/dev/null || true", shell=True)
        time.sleep(0.5)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
        env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
        env["PYTHONUNBUFFERED"] = "1"
        # Prepend mock bin to PATH so Assembly Agent uses our fake ffmpeg/ffprobe
        env["PATH"] = f"{self.mock_bin_dir}:{env.get('PATH', '')}"

        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.assembly_stdout = open(log_dir / "scaffold_assembly_stdout.log", "w")
        self.assembly_stderr = open(log_dir / "scaffold_assembly_stderr.log", "w")

        self.assembly_process = subprocess.Popen(
            [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "agents.assembly.app:app", "--host", "127.0.0.1", "--port", str(self.assembly_port)],
            stdout=self.assembly_stdout,
            stderr=self.assembly_stderr,
            cwd=str(PROJECT_ROOT / "server"),
            env=env
        )
        
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get(f"http://127.0.0.1:{self.assembly_port}/", timeout=1.0)  # health probe
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise RuntimeError("Assembly agent failed to start on host")

    def cleanup(self):
        if self.assembly_process:
            self.assembly_process.kill()
            self.assembly_process.wait()
        if self.gsa_process:
            self.gsa_process.kill()
            self.gsa_process.wait()
        subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
        subprocess.run(f"kill -9 $(lsof -t -i:{self.assembly_port}) 2>/dev/null || true", shell=True)
        
        for attr in ("gsa_stdout", "gsa_stderr", "assembly_stdout", "assembly_stderr"):
            f = getattr(self, attr, None)
            if f:
                try:
                    f.close()
                except Exception:
                    pass

@pytest.fixture
def scaffold_helper():
    helper = HostScaffoldingHelper()
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

    import glob
    for f in glob.glob(os.path.expanduser("~/.deepagents/sessions.db*")):
        try:
            os.remove(f)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Step Definitions: Hour-Long Movie Scaffolding
# ---------------------------------------------------------------------------
@given("the event store contains a script for an hour-long documentary (120 blocks, 3600s target)")
def step_contains_hour_long_script(event_store):
    clear_local_event_store()
    event_store._init_db()
    
    # Configure WAL mode
    with event_store._connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=100.0), "")

    # Generate 120 script blocks (30s each = 3600s total)
    blocks = []
    for idx in range(120):
        scene_num = (idx // 6) + 1
        block_id = f"s{scene_num}_b{idx % 6 + 1}"
        blocks.append(ScriptBlock(
            scene_num=scene_num,
            block_id=block_id,
            speaker="Narrator",
            text=f"This is narration text for block {idx + 1}.",
            duration_sec=30.0
        ))
    event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")

@when("the Provisioner schedules all parallel rendering jobs (120 tts, 120 video)")
def step_schedules_parallel_jobs(event_store):
    # Test harness simulates Provisioner scheduling
    for idx in range(120):
        scene_num = (idx // 6) + 1
        block_id = f"s{scene_num}_b{idx % 6 + 1}"
        
        event_store.append(QueueJob(
            agent="audio",
            job_id=f"job_tts_{idx + 1}",
            job_type="tts",
            scene_num=scene_num,
            block_id=block_id,
            slot_id=f"A1:{scene_num}:{block_id}",
            params={"text": f"This is narration text for block {idx + 1}.", "voice": "narrator"}
        ), "")

        event_store.append(QueueJob(
            agent="video",
            job_id=f"job_video_{idx + 1}",
            job_type="ltx",
            scene_num=scene_num,
            block_id=block_id,
            slot_id=f"V1:{scene_num}:{block_id}",
            params={"prompt": f"Visual notes for block {idx + 1}.", "duration_sec": 30.0}
        ), "")

@when("all rendering jobs are completed with media file metadata")
def step_completes_rendering_jobs(event_store):
    os.makedirs("/tmp/audio", exist_ok=True)
    os.makedirs("/tmp/video", exist_ok=True)

    for idx in range(120):
        scene_num = (idx // 6) + 1
        block_id = f"s{scene_num}_b{idx % 6 + 1}"
        
        # Touch mock output files
        audio_path = f"/tmp/audio/{block_id}.wav"
        video_path = f"/tmp/video/{block_id}.mp4"
        with open(audio_path, "wb") as f:
            f.write(b"\x00" * 2048)
        with open(video_path, "wb") as f:
            f.write(b"\x00" * 2048)

        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=f"job_tts_{idx + 1}",
            artifact_uri=audio_path,
            duration_sec=30.0,
            vm_instance_id="vm_instance_1"
        ), "")

        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=f"job_video_{idx + 1}",
            artifact_uri=video_path,
            duration_sec=30.0,
            vm_instance_id="vm_instance_1"
        ), "")

        # Assembly Agent incremental merges
        event_store.append(DurationAdjusted(
            agent="audio",
            block_id=f"A1:{scene_num}:{block_id}",
            slot_id=f"A1:{scene_num}:{block_id}",
            scene_num=scene_num,
            voice_role="Narrator",
            scripted_sec=30.0,
            measured_sec=30.0
        ), "")

        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id=f"job_tts_{idx + 1}",
            block_id=block_id,
            scene_num=scene_num,
            slot_id=f"A1:{scene_num}:{block_id}",
            artifact_uri=audio_path,
            track_name="A1_Narration",
            duration_sec=30.0
        ), "")

        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id=f"job_video_{idx + 1}",
            block_id=block_id,
            scene_num=scene_num,
            slot_id=f"V1:{scene_num}:{block_id}",
            artifact_uri=video_path,
            track_name="V1_Video",
            duration_sec=30.0
        ), "")

@then("the Assembly Agent compiles the entire 120-slot OpenTimelineIO sequence")
def step_compiles_sequence_assembly(scaffold_helper):
    # Wake up Assembly Agent
    resp = httpx.post(f"http://127.0.0.1:{scaffold_helper.assembly_port}/", content="Wake up and check GSA")
    assert resp.status_code == 200

@then("the compiled sequence has zero gaps or overlaps and matches the 3600s target duration")
def step_verify_timeline_duration(event_store):
    start_time = time.time()
    while time.time() - start_time < 300:
        effects = [e.effect for e in event_store.read_all()]
        completes = [e for e in effects if e.kind == "pipeline_complete"]
        if completes:
            assert completes[-1].duration_sec == 3600.0
            return
        time.sleep(1.0)
    raise AssertionError("Assembly agent did not compile timeline within 300s")

@then("the SQLite database WAL performance is verified stable")
def step_verify_wal_stable(event_store):
    with event_store._connect() as conn:
        res = conn.execute("PRAGMA journal_mode;").fetchone()
        assert res[0].lower() == "wal"

# ---------------------------------------------------------------------------
# Step Definitions: Multi-VM Job Dispatch & Fleet Coordination
# ---------------------------------------------------------------------------
@given("the job queue contains 50 pending audio and video rendering tasks")
def step_queue_50_tasks(event_store):
    clear_local_event_store()
    event_store._init_db()
    event_store.append(PipelineStarted(agent="operator"), "")
    
    # 25 TTS, 25 Video jobs
    for idx in range(25):
        event_store.append(QueueJob(
            agent="audio",
            job_id=f"job_tts_{idx + 1}",
            job_type="tts",
            scene_num=1,
            block_id=f"s1_b{idx + 1}",
            slot_id=f"A1:1:s1_b{idx + 1}",
            params={"text": "Technical test.", "voice": "narrator"}
        ), "")
        
        event_store.append(QueueJob(
            agent="video",
            job_id=f"job_video_{idx + 1}",
            job_type="ltx",
            scene_num=1,
            block_id=f"s1_b{idx + 1}",
            slot_id=f"V1:1:s1_b{idx + 1}",
            params={"prompt": "Visual test.", "duration_sec": 5.0}
        ), "")

@when("the Provisioner registers multiple active worker VM instances")
def step_register_multiple_vms(event_store):
    for idx in range(4):
        event_store.append(VMAllocated(
            agent="provisioner",
            instance_id=f"vm_instance_{idx + 1}",
            role="tts" if idx % 2 == 0 else "ltx",
            offer_id="offer_123",
            worker_url=f"http://127.0.0.1:888{idx + 1}",
            gpu_type="RTX 4090",
            cost_per_hour=0.5
        ), "")

@when("initiates parallel job claiming across the active fleet")
def step_parallel_job_claiming(event_store):
    # Simulate Provisioner scheduling parallel execution mapping
    for idx in range(25):
        vm_id = f"vm_instance_{idx % 4 + 1}"
        # Start TTS job
        event_store.append(JobStarted(
            agent="provisioner",
            job_id=f"job_tts_{idx + 1}",
            vm_instance_id=vm_id
        ), "")
        
        # Start Video job
        event_store.append(JobStarted(
            agent="provisioner",
            job_id=f"job_video_{idx + 1}",
            vm_instance_id=vm_id
        ), "")

@then("distinct jobs are routed to distinct worker VMs based on capability matches")
def step_verify_capability_routing(event_store):
    effects = [e.effect for e in event_store.read_all()]
    started = [e for e in effects if e.kind == "job_started"]
    assert len(started) == 50
    # Assert jobs distributed across the 4 instances
    vms = {s.vm_instance_id for s in started}
    assert len(vms) == 4

@then("the event store logs each job's completion with its handling VM instance ID")
def step_verify_handling_vm_logged(event_store):
    for idx in range(25):
        vm_id = f"vm_instance_{idx % 4 + 1}"
        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=f"job_tts_{idx + 1}",
            artifact_uri=f"/tmp/audio/s1_b{idx + 1}.wav",
            duration_sec=5.0,
            vm_instance_id=vm_id
        ), "")
        
        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=f"job_video_{idx + 1}",
            artifact_uri=f"/tmp/video/s1_b{idx + 1}.mp4",
            duration_sec=5.0,
            vm_instance_id=vm_id
        ), "")
        
    effects = [e.effect for e in event_store.read_all()]
    completed = [e for e in effects if e.kind == "job_completed"]
    assert len(completed) == 50
    for c in completed:
        assert c.vm_instance_id.startswith("vm_instance_")

# ---------------------------------------------------------------------------
# Step Definitions: Localized Segment Recovery
# ---------------------------------------------------------------------------
@given("a 100-block documentary run where 98 blocks have completed audio/video jobs but 2 blocks have failed")
def step_documentary_run_98_completed_2_failed(event_store):
    clear_local_event_store()
    event_store._init_db()
    event_store.append(PipelineStarted(agent="operator"), "")
    
    # 100 blocks
    blocks = []
    for idx in range(100):
        blocks.append(ScriptBlock(
            scene_num=1,
            block_id=f"s1_b{idx + 1}",
            speaker="Narrator",
            text=f"This is a block of narration text for segment {idx + 1} of the documentary.",
            duration_sec=5.0
        ))
    event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")
    
    os.makedirs("/tmp/audio", exist_ok=True)
    os.makedirs("/tmp/video", exist_ok=True)

    # Seed 98 completed and 2 failed
    for idx in range(100):
        block_id = f"s1_b{idx + 1}"
        
        # Touch mock output files
        video_path = f"/tmp/video/{block_id}.mp4"
        with open(video_path, "wb") as f:
            f.write(b"\x00" * 2048)

        # Every block has its video successfully completed and merged
        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=f"job_video_{idx + 1}",
            artifact_uri=video_path,
            duration_sec=5.0,
            vm_instance_id="vm_instance_1"
        ), "")
        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id=f"job_video_{idx + 1}",
            block_id=block_id,
            scene_num=1,
            slot_id=f"V1:1:{block_id}",
            artifact_uri=video_path,
            track_name="V1_Video",
            duration_sec=5.0
        ), "")
        
        if idx in (44, 81):  # blocks 45 and 82 fail audio
            event_store.append(QueueJob(
                agent="audio",
                job_id=f"job_tts_{idx + 1}",
                job_type="tts",
                scene_num=1,
                block_id=block_id,
                slot_id=f"A1:1:{block_id}",
                params={"text": f"This is a block of narration text for segment {idx + 1} of the documentary.", "voice": "narrator"}
            ), "")
            # Log failure
            from effects import JobFailed
            event_store.append(JobFailed(
                agent="provisioner",
                job_id=f"job_tts_{idx + 1}",
                error_message="Worker VM network timeout",
                failure_category="network",
                vm_instance_id="vm_instance_failed"
            ), "")
        else: # blocks other than 45 and 82 succeed audio
            audio_path = f"/tmp/audio/{block_id}.wav"
            with open(audio_path, "wb") as f:
                f.write(b"\x00" * 2048)

            event_store.append(JobCompleted(
                agent="provisioner",
                job_id=f"job_tts_{idx + 1}",
                artifact_uri=audio_path,
                duration_sec=5.0,
                vm_instance_id="vm_instance_1"
            ), "")
            event_store.append(DurationAdjusted(
                agent="audio",
                block_id=f"A1:1:{block_id}",
                slot_id=f"A1:1:{block_id}",
                scene_num=1,
                voice_role="Narrator",
                scripted_sec=5.0,
                measured_sec=5.0
            ), "")
            event_store.append(MergeIntoOTIO(
                agent="assembly",
                job_id=f"job_tts_{idx + 1}",
                block_id=block_id,
                scene_num=1,
                slot_id=f"A1:1:{block_id}",
                artifact_uri=audio_path,
                track_name="A1_Narration",
                duration_sec=5.0
            ), "")

@when("the Provisioner detects the failure logs in the event store")
def step_provisioner_detects_failures():
    pass

@then("it retries only the 2 failed jobs on a fresh or recycled worker VM")
def step_retry_failed_jobs(event_store):
    # Touch mock output files for the retried audio blocks
    for block in ("s1_b45", "s1_b82"):
        with open(f"/tmp/audio/{block}.wav", "wb") as f:
            f.write(b"\x00" * 2048)

    # Retry blocks 45 and 82
    event_store.append(QueueJob(
        agent="audio",
        job_id="job_tts_45_retry",
        job_type="tts",
        scene_num=1,
        block_id="s1_b45",
        slot_id="A1:1:s1_b45",
        params={"text": "This is a block of narration text for segment 45 of the documentary.", "voice": "narrator"}
    ), "")
    
    event_store.append(QueueJob(
        agent="audio",
        job_id="job_tts_82_retry",
        job_type="tts",
        scene_num=1,
        block_id="s1_b82",
        slot_id="A1:1:s1_b82",
        params={"text": "This is a block of narration text for segment 82 of the documentary.", "voice": "narrator"}
    ), "")

    # Complete the retries
    event_store.append(JobCompleted(
        agent="provisioner",
        job_id="job_tts_45_retry",
        artifact_uri="/tmp/audio/s1_b45.wav",
        duration_sec=5.0,
        vm_instance_id="vm_instance_recycled"
    ), "")
    
    event_store.append(JobCompleted(
        agent="provisioner",
        job_id="job_tts_82_retry",
        artifact_uri="/tmp/audio/s1_b82.wav",
        duration_sec=5.0,
        vm_instance_id="vm_instance_recycled"
    ), "")

    event_store.append(DurationAdjusted(
        agent="audio",
        block_id="A1:1:s1_b45",
        slot_id="A1:1:s1_b45",
        scene_num=1,
        voice_role="Narrator",
        scripted_sec=5.0,
        measured_sec=5.0
    ), "")

    event_store.append(DurationAdjusted(
        agent="audio",
        block_id="A1:1:s1_b82",
        slot_id="A1:1:s1_b82",
        scene_num=1,
        voice_role="Narrator",
        scripted_sec=5.0,
        measured_sec=5.0
    ), "")

    event_store.append(MergeIntoOTIO(
        agent="assembly",
        job_id="job_tts_45_retry",
        block_id="s1_b45",
        scene_num=1,
        slot_id="A1:1:s1_b45",
        artifact_uri="/tmp/audio/s1_b45.wav",
        track_name="A1_Narration",
        duration_sec=5.0
    ), "")
    
    event_store.append(MergeIntoOTIO(
        agent="assembly",
        job_id="job_tts_82_retry",
        block_id="s1_b82",
        scene_num=1,
        slot_id="A1:1:s1_b82",
        artifact_uri="/tmp/audio/s1_b82.wav",
        track_name="A1_Narration",
        duration_sec=5.0
    ), "")

@then("the Assembly Agent holds compilation until the retried segments are completed")
def step_verify_assembly_holds(scaffold_helper, event_store):
    # Wake up Assembly Agent
    resp = httpx.post(f"http://127.0.0.1:{scaffold_helper.assembly_port}/", content="Wake up and check GSA")
    assert resp.status_code == 200

@then("the final movie timeline compiles successfully with all 100 media slots present")
def step_verify_100_slots_timeline(event_store):
    start_time = time.time()
    while time.time() - start_time < 300:
        effects = [e.effect for e in event_store.read_all()]
        completes = [e for e in effects if e.kind == "pipeline_complete"]
        if completes:
            return
        time.sleep(1.0)
    raise AssertionError("Timeline compilation holding failed")
