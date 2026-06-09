import os
import sys
import subprocess
import socket
import httpx
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import PipelineStarted, UpdateScript, ScriptBlock, DurationAdjusted
from coordinate_timeline import CoordinateTimeline

def test_coordinate_timeline_dynamic_drift():
    print('\n▶️  [STARTING TEST] test_coordinate_timeline_dynamic_drift')
    
    # 1. Assert immediately that live credentials, network reachability, and physical binaries are present
    deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
    assert os.path.exists(deepseek_key_path), "CRITICAL FAILURE: DeepSeek API key file is missing!"
    assert os.path.exists(vast_key_path), "CRITICAL FAILURE: Vast.ai API key file is missing!"
    
    try:
        socket.create_connection(("vast.ai", 80), timeout=5.0)
    except Exception as e:
        raise AssertionError(f"CRITICAL FAILURE: Vast.ai server is unreachable: {e}")
        
    try:
        subprocess.run(["sqlite3", "-version"], capture_output=True, check=True)
    except Exception as e:
        raise AssertionError(f"CRITICAL FAILURE: sqlite3 CLI binary is missing or not callable: {e}")
        
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except Exception as e:
        raise AssertionError(f"CRITICAL FAILURE: ffmpeg binary is missing or not callable: {e}")
        
    # 2. Setup persistent SQLite database on physical disk at project root
    db_file = os.path.join(PROJECT_ROOT, "events.db")
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except Exception:
            pass
            
    event_store = EventStore(log_dir=str(PROJECT_ROOT))
    event_store._init_db()
    
    # Initialize script with 3 blocks
    blocks = [
        ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="First text.", duration_sec=3.0),
        ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator", text="Second text.", duration_sec=3.0),
        ScriptBlock(scene_num=1, block_id="s1_b3", speaker="narrator", text="Third text.", duration_sec=3.0)
    ]
    event_store.append(PipelineStarted(agent="operator", output_path=f"{PROJECT_ROOT}/final.mp4"), "")
    event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")
    
    # Spin up GSA in integration harness using project root as log dir
    with IntegrationHarness(required_agents=["gsa"], capabilities=[]) as harness:
        # Override harness temporary directory config with the persistent database path
        gsa_port = harness.ports["gsa"]
        gsa_url = f"http://127.0.0.1:{gsa_port}/"
        
        # Get duration before adjustment via live GSA HTTP GET
        resp_before = httpx.get(gsa_url)
        assert resp_before.status_code == 200
        state_before = resp_before.json()
        duration_before = float(state_before["otio"]["duration_sec"])
        assert duration_before == 9.0
        
        # Adjust duration of block 1 (increase by 2.0s to 5.0s)
        event_store.append(DurationAdjusted(
            agent="audio", block_id="s1_b1", slot_id="A1:1:s1_b1",
            scene_num=1, voice_role="narrator", scripted_sec=3.0, measured_sec=5.0
        ), "initial_hash")
        
        # Get duration after adjustment via live GSA HTTP GET
        resp_after = httpx.get(gsa_url)
        assert resp_after.status_code == 200
        state_after = resp_after.json()
        duration_after = float(state_after["otio"]["duration_sec"])
        
        # Assert that the total timeline duration increased exactly by 2.0 seconds (SC-08 BDD Scenario)
        assert duration_after - duration_before == 2.0
        assert duration_after == 11.0
        
        # Verify database contents using physical sqlite3 CLI command
        res = subprocess.run(["sqlite3", db_file, "SELECT seq, kind FROM events ORDER BY seq"], capture_output=True, text=True, check=True)
        assert "duration_adjusted" in res.stdout
        
        # Verify start/end coordinates of blocks 2 and 3 using the local CoordinateTimeline projection
        coord_timeline = CoordinateTimeline()
        coord_timeline.tick(event_store)
        
        c_clips = sorted(coord_timeline.clips["audio"], key=lambda c: c.span.start_sec)
        assert len(c_clips) == 3
        # Block 1 starts at 0.0s, ends at 5.0s (duration 5.0s)
        assert c_clips[0].scenario_id == "s1_b1"
        assert c_clips[0].span.start_sec == 0.0
        assert c_clips[0].span.end_sec == 5.0
        
        # Block 2 starts at 5.0s, ends at 8.0s (duration 3.0s)
        assert c_clips[1].scenario_id == "s1_b2"
        assert c_clips[1].span.start_sec == 5.0
        assert c_clips[1].span.end_sec == 8.0
        
        # Block 3 starts at 8.0s, ends at 11.0s (duration 3.0s)
        assert c_clips[2].scenario_id == "s1_b3"
        assert c_clips[2].span.start_sec == 8.0
        assert c_clips[2].span.end_sec == 11.0

        # Assert database-native high precision subtraction using sqlean (Condition 2)
        diff_ns = coord_timeline.query_sqlean_timespan(0.0, 11.0)
        assert diff_ns == 11 * 1000000000
        
        print("✓ Coordinate Timeline dynamic drift verified.")
    
    # Cleanup persistent database file
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except Exception:
            pass
