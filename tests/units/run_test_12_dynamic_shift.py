import os
import sys
import time
import subprocess
import httpx
from pathlib import Path

# Append server path to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from effects import (
    PipelineStarted,
    BudgetSet,
    UpdateScript,
    ScriptBlock,
    JobCompleted,
    DurationAdjusted,
    MergeIntoOTIO,
    ReconciliationComplete,
)
from event_store import EventStore

def clear_local_event_store():
    import shutil
    db_dir = "/tmp/documentary-pipeline"
    try:
        shutil.rmtree(db_dir)
    except Exception:
        pass
    os.makedirs(db_dir, exist_ok=True)

def start_server(name: str, port: int, env: dict):
    subprocess.run(f"kill -9 $(lsof -t -i:{port}) 2>/dev/null || true", shell=True)
    time.sleep(0.5)
    app_module = "global_state_agent:app" if name == "gsa" else f"agents.{name}.app:app"
    return subprocess.Popen(
        [str(PROJECT_ROOT / ".venv/bin/uvicorn"), app_module, "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_ROOT / "server"),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

def main():
    print("=== STARTING TEST 12: DYNAMIC SHIFT CASCADE INTEGRATION ===")
    clear_local_event_store()
    
    event_store = EventStore(log_dir="/tmp/documentary-pipeline")
    event_store._init_db()
    with event_store._connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")

    # Seed the initial run config and budget
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")

    # We manually seed the base screenplay script: two blocks of scripted duration 3.0s
    block_1 = ScriptBlock(
        scene_num=1,
        block_id="s1_b1",
        speaker="V1_Narrator",
        text="Jacques Lacan was born in Paris.",
        duration_sec=3.0
    )
    block_2 = ScriptBlock(
        scene_num=1,
        block_id="s1_b2",
        speaker="V1_Narrator",
        text="He had a strict Catholic upbringing.",
        duration_sec=3.0
    )
    event_store.append(UpdateScript(agent="scenario", blocks=[block_1, block_2]), "initial_hash")

    # Start servers
    servers = {
        "gsa": 8000,
        "audio": 8002,
        "video": 8004,
        "assembly": 8005
    }
    
    processes = []
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
    env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
    env["PYTHONUNBUFFERED"] = "1"

    try:
        for name, port in servers.items():
            print(f"Spawning {name} agent on port {port}...")
            p = start_server(name, port, env)
            processes.append(p)
            
            # Wait for health
            delay = 0.2
            for _ in range(15):
                try:
                    resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)
                    if resp.status_code == 200:
                        break
                except Exception:
                    pass
                    time.sleep(delay)
                    delay = min(delay * 1.5, 2.0)

        # 1. Trigger Audio Agent wakeup to queue the TTS jobs
        print("Waking up Audio Agent...")
        httpx.post("http://127.0.0.1:8002/", content="Wake up and check GSA")

        # Wait for the Audio Agent to queue the first TTS job
        job_audio_1_id = None
        print("Waiting for TTS job queue event...")
        for _ in range(60):
            effects = [e.effect for e in event_store.read_all()]
            audio_jobs = [e for e in effects if e.kind == "queue_job" and e.agent == "audio"]
            if audio_jobs:
                job_audio_1_id = audio_jobs[0].job_id
                print(f"Detected TTS job: {job_audio_1_id}")
                break
            time.sleep(1.0)
        
        assert job_audio_1_id is not None, "Audio Agent failed to queue TTS job"

        # 2. Simulate TTS worker returning with a DURATION SHIFT (4.0s instead of 3.0s)
        print("Simulating TTS worker completing job with duration shift (4.0s)...")
        audio_path = "/tmp/audio/s1_b1.wav"
        os.makedirs("/tmp/audio", exist_ok=True)
        # Create a real 4.0s audio segment using ffmpeg
        subprocess.run(
            f"ffmpeg -y -f lavfi -i sine=f=1000:r=44100 -t 4.0 {audio_path}",
            shell=True,
            capture_output=True
        )
        
        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=job_audio_1_id,
            artifact_uri=audio_path,
            duration_sec=4.0,
            vm_instance_id="vm_instance_mock"
        ), "initial_hash")
        
        # Audio agent adjusts the duration target of block 1
        event_store.append(DurationAdjusted(
            agent="audio",
            block_id="A1:1:s1_b1",
            slot_id="A1:1:s1_b1",
            scene_num=1,
            voice_role="V1_Narrator",
            scripted_sec=3.0,
            measured_sec=4.0
        ), "initial_hash")
        
        # Merge the first audio clip into OTIO
        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id=job_audio_1_id,
            block_id="s1_b1",
            scene_num=1,
            slot_id="A1:1:s1_b1",
            artifact_uri=audio_path,
            track_name="A1_Narration",
            duration_sec=4.0,
            start_sec=0.0
        ), "initial_hash")

        # Now trigger the second block's TTS job
        print("Waking up Audio Agent for block 2...")
        httpx.post("http://127.0.0.1:8002/", content="Wake up and check GSA")

        # Wait for the second job to queue
        job_audio_2_id = None
        for _ in range(60):
            effects = [e.effect for e in event_store.read_all()]
            audio_jobs = [e for e in effects if e.kind == "queue_job" and e.agent == "audio" and e.block_id == "s1_b2"]
            if audio_jobs:
                job_audio_2_id = audio_jobs[0].job_id
                print(f"Detected block 2 TTS job: {job_audio_2_id}")
                break
            time.sleep(1.0)
        
        assert job_audio_2_id is not None, "Audio Agent failed to queue block 2 TTS job"

        # Complete block 2 normally (3.0s duration)
        print("Simulating TTS worker completing job 2 (3.0s)...")
        audio_2_path = "/tmp/audio/s1_b2.wav"
        subprocess.run(
            f"ffmpeg -y -f lavfi -i sine=f=1000:r=44100 -t 3.0 {audio_2_path}",
            shell=True,
            capture_output=True
        )
        
        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=job_audio_2_id,
            artifact_uri=audio_2_path,
            duration_sec=3.0,
            vm_instance_id="vm_instance_mock"
        ), "initial_hash")
        
        event_store.append(DurationAdjusted(
            agent="audio",
            block_id="A1:1:s1_b2",
            slot_id="A1:1:s1_b2",
            scene_num=1,
            voice_role="V1_Narrator",
            scripted_sec=3.0,
            measured_sec=3.0
        ), "initial_hash")
        
        # Merge the second audio clip into OTIO at the cascade shifted start offset (4.0s)
        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id=job_audio_2_id,
            block_id="s1_b2",
            scene_num=1,
            slot_id="A1:1:s1_b2",
            artifact_uri=audio_2_path,
            track_name="A1_Narration",
            duration_sec=3.0,
            start_sec=4.0
        ), "initial_hash")

        # Emit reconciliation complete
        event_store.append(ReconciliationComplete(
            agent="audio",
            blocks_total=2,
            blocks_passed=2,
            blocks_failed=0,
            worst_delta_sec=0.0,
            total_measured_sec=7.0
        ), "initial_hash")

        # 3. Assert dynamic offset shift via GSA
        print("Querying GSA to verify coordinate timeline cascade shift...")
        resp = httpx.get("http://127.0.0.1:8000/")
        assert resp.status_code == 200, "GSA failed to respond"
        state = resp.json()
        
        duration = state.get("otio", {}).get("duration_sec")
        print(f"Current GSA timeline duration: {duration}s")
        assert abs(duration - 7.0) < 0.01, f"Expected 7.0s total duration, got {duration}s"

        # 4. Trigger Video Agent wakeup
        print("Waking up Video Agent...")
        httpx.post("http://127.0.0.1:8004/", content="Wake up and check GSA")

        # Wait for Video Agent to queue video rendering jobs
        print("Waiting for Video jobs to be queued...")
        job_video_1_id = None
        job_video_2_id = None
        for _ in range(60):
            effects = [e.effect for e in event_store.read_all()]
            video_jobs = [e for e in effects if e.kind == "queue_job" and e.agent == "video"]
            if len(video_jobs) >= 2:
                job_video_1_id = video_jobs[0].job_id
                job_video_2_id = video_jobs[1].job_id
                print(f"Detected video jobs: {job_video_1_id}, {job_video_2_id}")
                break
            time.sleep(1.0)

        assert job_video_1_id is not None and job_video_2_id is not None, "Video Agent failed to queue jobs"

        # Simulate completion of shifted video clips matching the shifted coordinates:
        # Video 1: 0.0s to 4.0s (4.0s duration)
        # Video 2: 4.0s to 7.0s (3.0s duration)
        print("Simulating video render completions at shifted coordinates...")
        video_1_path = "/tmp/video/s1_b1.mp4"
        video_2_path = "/tmp/video/s1_b2.mp4"
        os.makedirs("/tmp/video", exist_ok=True)
        subprocess.run(
            f"ffmpeg -y -f lavfi -i color=c=red:s=320x240:d=4.0 -c:v libx264 -pix_fmt yuv420p {video_1_path}",
            shell=True, capture_output=True
        )
        subprocess.run(
            f"ffmpeg -y -f lavfi -i color=c=blue:s=320x240:d=3.0 -c:v libx264 -pix_fmt yuv420p {video_2_path}",
            shell=True, capture_output=True
        )

        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=job_video_1_id,
            artifact_uri=video_1_path,
            duration_sec=4.0,
            vm_instance_id="vm_instance_mock"
        ), "initial_hash")
        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id=job_video_1_id,
            block_id="s1_b1",
            scene_num=1,
            slot_id="V1:1:s1_b1",
            artifact_uri=video_1_path,
            track_name="V1_Video",
            duration_sec=4.0,
            start_sec=0.0
        ), "initial_hash")

        event_store.append(JobCompleted(
            agent="provisioner",
            job_id=job_video_2_id,
            artifact_uri=video_2_path,
            duration_sec=3.0,
            vm_instance_id="vm_instance_mock"
        ), "initial_hash")
        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id=job_video_2_id,
            block_id="s1_b2",
            scene_num=1,
            slot_id="V1:1:s1_b2",
            artifact_uri=video_2_path,
            track_name="V1_Video",
            duration_sec=3.0,
            start_sec=4.0
        ), "initial_hash")

        # 5. Trigger Assembly Agent wakeup to compile final movie
        print("Waking up Assembly Agent...")
        httpx.post("http://127.0.0.1:8005/", content="Wake up and check GSA")

        # Wait for pipeline completion
        print("Waiting for pipeline_complete event...")
        output_movie_path = None
        for _ in range(60):
            effects = [e.effect for e in event_store.read_all()]
            completes = [e for e in effects if e.kind == "pipeline_complete"]
            if completes:
                output_movie_path = completes[-1].output_path
                print(f"Pipeline complete! Output movie at: {output_movie_path}")
                break
            time.sleep(1.0)

        assert output_movie_path is not None, "Assembly Agent failed to produce final output"
        assert os.path.exists(output_movie_path), "Output MP4 file does not exist physically"

        # 6. Verify final compiled movie duration using ffprobe
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", output_movie_path],
            capture_output=True,
            text=True
        )
        final_duration = float(res.stdout.strip())
        print(f"Final compiled movie duration measured by ffprobe: {final_duration}s")
        assert abs(final_duration - 7.0) < 0.1, f"Expected exactly 7.0s duration movie, got {final_duration}s"
        
        print("\n=== TEST 12 PASSED SUCCESSFULLY ===")

    finally:
        print("Cleaning up server processes...")
        for p in processes:
            p.terminate()
            p.wait()
        subprocess.run("kill -9 $(lsof -t -i:8000,8002,8004,8005) 2>/dev/null || true", shell=True)

if __name__ == "__main__":
    main()
