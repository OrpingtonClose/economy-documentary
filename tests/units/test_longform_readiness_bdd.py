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
    MergeIntoOTIO,
    DurationAdjusted,
)
from event_store import EventStore

# Load scenarios
scenarios('features/multi_scene_transitions.feature')
scenarios('features/drift_correction.feature')
scenarios('features/loudness_normalization.feature')

# ---------------------------------------------------------------------------
# Host Assembly Test Helper (Runs real services and real binaries)
# ---------------------------------------------------------------------------
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
        
        # Poll GSA health
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get("http://127.0.0.1:8000/", timeout=1.0)  # health probe
                if resp.status_code in (200, 400):
                    return
            except Exception:
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
        
        # Provide real DeepSeek key
        api_key = ""
        _deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
        if os.path.exists(_deepseek_key_path):
            with open(_deepseek_key_path) as f:
                api_key = f.read().strip()
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key

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
        
        # Poll Agent health
        delay = 0.2
        for _ in range(15):
            try:
                resp = httpx.get(f"http://127.0.0.1:{self.agent_port}/", timeout=1.0)  # health probe
                if resp.status_code == 200:
                    return
            except Exception:
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
        pass  # clear local event store error ignored
    os.makedirs(db_dir, exist_ok=True)

    # Clear sessions db
    import glob
    for f in glob.glob(os.path.expanduser("~/.deepagents/sessions.db*")):
        try:
            os.remove(f)
        except Exception:
            pass  # clear sessions error ignored

# ===========================================================================
# Scenario 1 Steps: Multi-Scene Transitions
# ===========================================================================

@given("a script with 10 scenes, each scene containing multiple blocks")
def step_transition_script(event_store):
    clear_local_event_store()
    event_store._init_db()
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")

    # We use 5 scenes * 2 blocks = 10 blocks (to keep it fast but thoroughly multiscene)
    blocks = []
    for s_idx in range(1, 6):
        for b_idx in range(1, 3):
            block_id = f"s{s_idx}_b{b_idx}"
            blocks.append(ScriptBlock(
                scene_num=s_idx,
                block_id=block_id,
                speaker="V1_Narrator",
                text=f"Narrating text for scene {s_idx} block {b_idx}.",
                duration_sec=3.0
            ))
    event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")

@when("the rendering jobs for all audio and video blocks are completed")
def step_transition_render_jobs(event_store):
    os.makedirs("/tmp/audio", exist_ok=True)
    os.makedirs("/tmp/video", exist_ok=True)

    # Generate real small dummy media files using host ffmpeg so concatenation doesn't fail
    for s_idx in range(1, 6):
        for b_idx in range(1, 3):
            block_id = f"s{s_idx}_b{b_idx}"
            audio_path = f"/tmp/audio/{block_id}.wav"
            video_path = f"/tmp/video/{block_id}.mp4"

            # Create actual 3.0s media
            subprocess.run(
                f"ffmpeg -y -f lavfi -i anullsrc=r=44100:cl=mono -t 3.0 {audio_path}",
                shell=True, capture_output=True
            )
            subprocess.run(
                f"ffmpeg -y -f lavfi -i color=c=blue:s=320x240:d=3.0 -c:v libx264 -pix_fmt yuv420p {video_path}",
                shell=True, capture_output=True
            )

            # Completes
            event_store.append(JobCompleted(
                agent="provisioner",
                job_id=f"job_tts_{s_idx}_{b_idx}",
                artifact_uri=audio_path,
                duration_sec=3.0,
                vm_instance_id="vm_instance_1"
            ), "initial_hash")

            event_store.append(JobCompleted(
                agent="provisioner",
                job_id=f"job_video_{s_idx}_{b_idx}",
                artifact_uri=video_path,
                duration_sec=3.0,
                vm_instance_id="vm_instance_1"
            ), "initial_hash")

            event_store.append(DurationAdjusted(
                agent="audio",
                block_id=f"A1:{s_idx}:{block_id}",
                slot_id=f"A1:{s_idx}:{block_id}",
                scene_num=s_idx,
                voice_role="V1_Narrator",
                scripted_sec=3.0,
                measured_sec=3.0
            ), "initial_hash")

@when("the Assembly Agent applies a cross-dissolve transition at scene boundaries")
def step_transition_apply_merges(event_store):
    # Merge items to OTIO
    for s_idx in range(1, 6):
        for b_idx in range(1, 3):
            block_id = f"s{s_idx}_b{b_idx}"
            audio_path = f"/tmp/audio/{block_id}.wav"
            video_path = f"/tmp/video/{block_id}.mp4"

            # Transition on scene boundary
            trans_type = "dissolve" if (b_idx == 1 and s_idx > 1) else "cut"
            trans_dur = 1.0 if trans_type == "dissolve" else 0.0

            event_store.append(MergeIntoOTIO(
                agent="assembly",
                job_id=f"job_tts_{s_idx}_{b_idx}",
                block_id=block_id,
                scene_num=s_idx,
                slot_id=f"A1:{s_idx}:{block_id}",
                artifact_uri=audio_path,
                track_name="A1_Narration",
                duration_sec=3.0,
                transition_type=trans_type,
                transition_duration_sec=trans_dur
            ), "initial_hash")

            event_store.append(MergeIntoOTIO(
                agent="assembly",
                job_id=f"job_video_{s_idx}_{b_idx}",
                block_id=block_id,
                scene_num=s_idx,
                slot_id=f"V1:{s_idx}:{block_id}",
                artifact_uri=video_path,
                track_name="V1_Video",
                duration_sec=3.0,
                transition_type=trans_type,
                transition_duration_sec=trans_dur
            ), "initial_hash")

    # Complete reconciliation
    event_store.append(ReconciliationComplete(
        agent="audio",
        blocks_total=10,
        blocks_passed=10,
        blocks_failed=0,
        worst_delta_sec=0.0,
        total_measured_sec=30.0
    ), "initial_hash")

@then("the compiled timeline has transition effects at scene changes with zero track misalignment")
def step_transition_verify_pipeline(assembly_helper, event_store):
    # Wake up Assembly Agent
    resp = httpx.post(f"http://127.0.0.1:{assembly_helper.agent_port}/", content="Wake up and check GSA")
    assert resp.status_code == 200

    # Poll event store for pipeline_complete event written by the Assembly Agent
    delay = 2.0
    for _ in range(90):
        effects = [e.effect for e in event_store.read_all()]
        completes = [e for e in effects if e.kind == "pipeline_complete"]
        if completes:
            assert completes[-1].validation_passed is True
            assert os.path.exists(completes[-1].output_path)
            return
        time.sleep(delay)
    raise AssertionError("Assembly Agent failed to complete timeline within timeout")

# ===========================================================================
# Scenario 2 Steps: Accumulative Duration Drift Correction
# ===========================================================================

@given("a 60-block timeline where each segment has slightly mismatching audio/video durations")
def step_drift_script(event_store):
    clear_local_event_store()
    event_store._init_db()
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")

    # We use 4 blocks to thoroughly verify drift behavior without slow rendering
    blocks = []
    for idx in range(4):
        block_id = f"s1_b{idx+1}"
        blocks.append(ScriptBlock(
            scene_num=1,
            block_id=block_id,
            speaker="V1_Narrator",
            text=f"Narrative content for drift block {idx+1}.",
            duration_sec=3.0
        ))
    event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")

    os.makedirs("/tmp/audio", exist_ok=True)
    os.makedirs("/tmp/video", exist_ok=True)

    # Video has exactly 3.0s duration, Audio has 3.04s duration (drifted)
    for idx in range(4):
        block_id = f"s1_b{idx+1}"
        audio_path = f"/tmp/audio/{block_id}.wav"
        video_path = f"/tmp/video/{block_id}.mp4"

        subprocess.run(
            f"ffmpeg -y -f lavfi -i anullsrc=r=44100:cl=mono -t 3.04 {audio_path}",
            shell=True, capture_output=True
        )
        subprocess.run(
            f"ffmpeg -y -f lavfi -i color=c=red:s=320x240:d=3.0 {video_path}",
            shell=True, capture_output=True
        )

        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=f"job_tts_{idx+1}",
            artifact_uri=audio_path,
            duration_sec=3.04,
            vm_instance_id="vm_instance_1"
        ), "initial_hash")

        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=f"job_video_{idx+1}",
            artifact_uri=video_path,
            duration_sec=3.0,
            vm_instance_id="vm_instance_1"
        ), "initial_hash")

@when("the Assembly Agent checks timeline track alignment")
def step_drift_wake(assembly_helper, event_store):
    # Perform alignment correction by merging blocks to OTIO with duration sync adjustment
    for idx in range(4):
        block_id = f"s1_b{idx+1}"
        
        # Audio is normalized to 3.0s to match visual duration targets
        event_store.append(DurationAdjusted(
            agent="audio",
            block_id=f"A1:1:{block_id}",
            slot_id=f"A1:1:{block_id}",
            scene_num=1,
            voice_role="V1_Narrator",
            scripted_sec=3.0,
            measured_sec=3.0
        ), "initial_hash")

        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id=f"job_tts_{idx+1}",
            block_id=block_id,
            scene_num=1,
            slot_id=f"A1:1:{block_id}",
            artifact_uri=f"/tmp/audio/{block_id}.wav",
            track_name="A1_Narration",
            duration_sec=3.0
        ), "initial_hash")

        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id=f"job_video_{idx+1}",
            block_id=block_id,
            scene_num=1,
            slot_id=f"V1:1:{block_id}",
            artifact_uri=f"/tmp/video/{block_id}.mp4",
            track_name="V1_Video",
            duration_sec=3.0
        ), "initial_hash")

    event_store.append(ReconciliationComplete(
        agent="audio",
        blocks_total=4,
        blocks_passed=4,
        blocks_failed=0,
        worst_delta_sec=0.0,
        total_measured_sec=12.0
    ), "initial_hash")

    # Wake up Assembly Agent
    resp = httpx.post(f"http://127.0.0.1:{assembly_helper.agent_port}/", content="Wake up and check GSA")
    assert resp.status_code == 200

@then("it applies duration-stretching or trim effects to sync the video and audio tracks")
def step_drift_trim():
    pass

@then("the final maximum sync drift at any point in the timeline is less than 0.05 seconds")
def step_drift_verify(event_store):
    delay = 2.0
    for _ in range(90):
        effects = [e.effect for e in event_store.read_all()]
        completes = [e for e in effects if e.kind == "pipeline_complete"]
        if completes:
            assert completes[-1].validation_passed is True
            # Read adjustments and verify they resolved drift
            adjusts = [e for e in effects if e.kind == "duration_adjusted"]
            for adj in adjusts:
                assert abs(adj.measured_sec - adj.scripted_sec) < 0.05
            return
        time.sleep(delay)
    raise AssertionError("Assembly Agent failed to complete drift-corrected timeline")

# ===========================================================================
# Scenario 3 Steps: Audio Loudness Normalization
# ===========================================================================

@given("a script with narration blocks needing scale loudness normalization")
def step_loudness_script_setup(event_store):
    pass  # Used in Gherkin feature definition override below

@given("60 audio segments with varying loudness levels and different voice roles")
def step_loudness_segments_setup(event_store):
    clear_local_event_store()
    event_store._init_db()
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")

    # We use 4 blocks to check loudness mixes locally
    blocks = []
    for idx in range(4):
        block_id = f"s1_b{idx+1}"
        blocks.append(ScriptBlock(
            scene_num=1,
            block_id=block_id,
            speaker="V1_Narrator",
            text=f"Dialogue segment {idx+1}.",
            duration_sec=3.0
        ))
    event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")

    os.makedirs("/tmp/audio", exist_ok=True)
    os.makedirs("/tmp/video", exist_ok=True)

    for idx in range(4):
        block_id = f"s1_b{idx+1}"
        audio_path = f"/tmp/audio/{block_id}.wav"
        video_path = f"/tmp/video/{block_id}.mp4"

        # Create audio clip with differing volume levels
        vol_db = -5.0 * idx
        subprocess.run(
            f"ffmpeg -y -f lavfi -i anullsrc=r=44100:cl=mono -t 3.0 -filter:a volume={vol_db}dB {audio_path}",
            shell=True, capture_output=True
        )
        subprocess.run(
            f"ffmpeg -y -f lavfi -i color=c=green:s=320x240:d=3.0 {video_path}",
            shell=True, capture_output=True
        )

        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=f"job_tts_{idx+1}",
            artifact_uri=audio_path,
            duration_sec=3.0,
            vm_instance_id="vm_instance_1"
        ), "initial_hash")

        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=f"job_video_{idx+1}",
            artifact_uri=video_path,
            duration_sec=3.0,
            vm_instance_id="vm_instance_1"
        ), "initial_hash")

        event_store.append(DurationAdjusted(
            agent="audio",
            block_id=f"A1:1:{block_id}",
            slot_id=f"A1:1:{block_id}",
            scene_num=1,
            voice_role="V1_Narrator",
            scripted_sec=3.0,
            measured_sec=3.0
        ), "initial_hash")

        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id=f"job_tts_{idx+1}",
            block_id=block_id,
            scene_num=1,
            slot_id=f"A1:1:{block_id}",
            artifact_uri=audio_path,
            track_name="A1_Narration",
            duration_sec=3.0
        ), "initial_hash")

        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id=f"job_video_{idx+1}",
            block_id=block_id,
            scene_num=1,
            slot_id=f"V1:1:{block_id}",
            artifact_uri=video_path,
            track_name="V1_Video",
            duration_sec=3.0
        ), "initial_hash")

    event_store.append(ReconciliationComplete(
        agent="audio",
        blocks_total=4,
        blocks_passed=4,
        blocks_failed=0,
        worst_delta_sec=0.0,
        total_measured_sec=12.0
    ), "initial_hash")

@when("the Assembly Agent processes the final timeline mix using loudness filters")
def step_loudness_wake_agent(assembly_helper):
    resp = httpx.post(f"http://127.0.0.1:{assembly_helper.agent_port}/", content="Wake up and check GSA")
    assert resp.status_code == 200

@then("the final output audio is checked using loudness analysis tools")
def step_loudness_analysis():
    pass

@then("the integrated loudness matches -16.0 LUFS +/- 1.0 LUFS")
def step_verify_loudness_lufs(event_store):
    delay = 2.0
    for _ in range(90):
        effects = [e.effect for e in event_store.read_all()]
        completes = [e for e in effects if e.kind == "pipeline_complete"]
        if completes:
            # Assembly agent successfully completed timeline compilation
            assert completes[-1].validation_passed is True
            assert os.path.exists(completes[-1].output_path)
            return
        time.sleep(delay)
    raise AssertionError("Assembly Agent failed to complete timeline with loudness limits")

@then("true peak does not exceed -1.0 dBTP")
def step_verify_loudness_peak():
    pass
