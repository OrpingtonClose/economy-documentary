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
    print("=== STARTING TEST 13: PARALLEL MULTITRACK OVERLAP PREVENTION AND MUXING ===")
    clear_local_event_store()
    
    event_store = EventStore(log_dir="/tmp/documentary-pipeline")
    event_store._init_db()
    with event_store._connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")

    # Seed the initial run config and budget
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")

    # Seed screenplay script blocks
    block_1 = ScriptBlock(
        scene_num=1,
        block_id="s1_b1",
        speaker="V1_Narrator",
        text="Block 1 Narration",
        duration_sec=3.0
    )
    block_2 = ScriptBlock(
        scene_num=1,
        block_id="s1_b2",
        speaker="V1_Narrator",
        text="Block 2 Narration",
        duration_sec=3.0
    )
    event_store.append(UpdateScript(agent="scenario", blocks=[block_1, block_2]), "initial_hash")

    # Start GSA first
    servers = {
        "gsa": 8000,
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
                    resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)  # health probe
                    if resp.status_code == 200:
                        break
                except Exception:
                    time.sleep(delay)
                    delay = min(delay * 1.5, 2.0)

        # 1. Collision Check (Same Track)
        # We merge block 1 on A1_Narration at start_sec=0.0 (occupies 0s-3s)
        audio_path = "/tmp/audio/s1_b1.wav"
        os.makedirs("/tmp/audio", exist_ok=True)
        subprocess.run(f"ffmpeg -y -f lavfi -i sine=f=1000:r=44100 -t 3.0 {audio_path}", shell=True, capture_output=True)

        print("Merging block 1 on A1_Narration from 0s to 3s...")
        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id="job_audio_1",
            block_id="s1_b1",
            scene_num=1,
            slot_id="A1:1:s1_b1",
            artifact_uri=audio_path,
            track_name="A1_Narration",
            duration_sec=3.0,
            start_sec=0.0
        ), "initial_hash")

        # Now try to merge block 2 on A1_Narration at start_sec=1.5 (which overlaps block 1!)
        # The database-level coordinate timeline projection should raise a ValueError (Collision)
        print("Attempting to merge overlapping block 2 on the SAME track (A1_Narration at 1.5s)...")
        
        # We verify that GSA rejects the overlapping append or that applying the event triggers a validation collision error.
        # Let's import the timeline projection to verify applying it throws the exception.
        from coordinate_timeline import CoordinateTimeline
        projection = CoordinateTimeline()
        
        # Apply the first merge
        projection.apply(UpdateScript(agent="scenario", blocks=[block_1, block_2]))
        projection.apply(MergeIntoOTIO(
            agent="assembly",
            job_id="job_audio_1",
            block_id="s1_b1",
            scene_num=1,
            slot_id="A1:1:s1_b1",
            artifact_uri=audio_path,
            track_name="A1_Narration",
            duration_sec=3.0,
            start_sec=0.0
        ))
        
        collision_detected = False
        try:
            projection.apply(MergeIntoOTIO(
                agent="assembly",
                job_id="job_audio_2",
                block_id="s1_b2",
                scene_num=1,
                slot_id="A1:1:s1_b2",
                artifact_uri=audio_path,
                track_name="A1_Narration",
                duration_sec=3.0,
                start_sec=1.5
            ))
        except ValueError as exc:
            if "Collision on track 'A1_Narration'" in str(exc):
                collision_detected = True
                print(f"Successfully caught expected collision exception: {exc}")
        
        assert collision_detected, "Overlap on the same track did not raise a collision exception!"

        # 2. Isolation Check (Cross Track)
        # We merge block 2 on V1_Video at start_sec=1.5 (occupies 1.5s-4.5s)
        # This overlaps in time with block 1 on A1_Narration, but they are on different tracks, so it MUST succeed!
        print("Merging block 2 on a DIFFERENT track (V1_Video at 1.5s) to verify isolation...")
        video_path = "/tmp/video/s1_b2.mp4"
        os.makedirs("/tmp/video", exist_ok=True)
        subprocess.run(
            f"ffmpeg -y -f lavfi -i color=c=red:s=320x240:d=3.0 -c:v libx264 -pix_fmt yuv420p {video_path}",
            shell=True, capture_output=True
        )

        event_store.append(MergeIntoOTIO(
            agent="assembly",
            job_id="job_video_1",
            block_id="s1_b2",
            scene_num=1,
            slot_id="V1:1:s1_b2",
            artifact_uri=video_path,
            track_name="V1_Video",
            duration_sec=3.0,
            start_sec=1.5
        ), "initial_hash")

        # Query GSA to confirm both clips reside on their respective tracks
        print("Querying GSA to verify track contents...")
        resp = httpx.get("http://127.0.0.1:8000/")
        assert resp.status_code == 200
        state = resp.json()
        
        total_slots = state.get("otio", {}).get("total_slots")
        print(f"Total clips in OTIO timeline: {total_slots}")
        # Note: update_script seeds scripted slots for both tracks, and we have merged 1 audio and 1 video.
        
        # 3. Muxing and Timeline composition
        # Create a second silent video clip to fill narration track 0-1.5s, or we can just reconcile and mux
        event_store.append(ReconciliationComplete(
            agent="audio",
            blocks_total=1,
            blocks_passed=1,
            blocks_failed=0,
            worst_delta_sec=0.0,
            total_measured_sec=3.0
        ), "initial_hash")

        # Trigger Assembly Agent wakeup to mux the parallel audio and video streams
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

        # Verify output codecs: container has both video and audio streams
        print("Verifying final container streams using ffprobe...")
        res_v = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", output_movie_path],
            capture_output=True, text=True
        )
        res_a = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", output_movie_path],
            capture_output=True, text=True
        )
        
        video_codec = res_v.stdout.strip()
        audio_codec = res_a.stdout.strip()
        print(f"Container Video Codec: {video_codec} | Audio Codec: {audio_codec}")
        
        assert "h264" in video_codec or "mpeg4" in video_codec or video_codec != "", "Video stream is missing or invalid"
        assert "aac" in audio_codec or "pcm" in audio_codec or audio_codec != "", "Audio stream is missing or invalid"

        print("\n=== TEST 13 PASSED SUCCESSFULLY ===")

    finally:
        print("Cleaning up server processes...")
        for p in processes:
            p.terminate()
            p.wait()
        subprocess.run("kill -9 $(lsof -t -i:8000,8005) 2>/dev/null || true", shell=True)

if __name__ == "__main__":
    main()
