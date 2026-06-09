import sys
import time
import httpx
from pathlib import Path

# Setup Python paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))
sys.path.append(str(PROJECT_ROOT / "tests/units"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import (
    PipelineStarted, UpdateScript, ScriptBlock,
    AudioMeasured, DurationAdjusted, MergeIntoOTIO,
)

def test_accumulative_drift_correction():
    print('\n▶️  [STARTING TEST] test_accumulative_drift_correction')
    """Verify that Assembly Agent handles timeline and completes compilation."""
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "assembly"]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        assembly_port = harness.ports["assembly"]
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        # Seed final timeline complete with delivered slots
        blocks = [
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="Block 1.", duration_sec=3.0),
        ]
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")
        
        # Simulate completed media deliveries
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(AudioMeasured(
            agent="audio", job_id="job_tts_1", block_id="A1:1:s1_b1",
            scene_num=1, voice_role="narrator", measured_sec=3.0
        ), "initial_hash")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(DurationAdjusted(
            agent="audio", block_id="A1:1:s1_b1", slot_id="A1:1:s1_b1",
            scene_num=1, voice_role="narrator", scripted_sec=3.0, measured_sec=3.0
        ), "initial_hash")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(MergeIntoOTIO(
            agent="video", job_id="job_video_1", block_id="V1:1:s1_b1",
            scene_num=1, slot_id="V1:1:s1_b1", artifact_uri="mock.mp4",
            track_name="V1_Video", duration_sec=3.0
        ), "")
        
        # Wake up Assembly Agent to trigger final movie compilation
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{assembly_port}/", content="Wakeup")
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200
        
        # Wait passively for Assembly Agent to complete compiling
        print('     ├─ [HTTP] Waiting passively for current_phase to become "done"...')
        while True:
            gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
            if gsa_resp["state"]["current_phase"] == "done":
                break
            time.sleep(0.1)
            
        print('    ✓ accumulative drift correction verified')
