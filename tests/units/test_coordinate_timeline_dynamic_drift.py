import os
import sys
import subprocess
import httpx
import sqlean
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))
sys.path.append(str(PROJECT_ROOT / "tests/units"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import PipelineStarted, UpdateScript, ScriptBlock, DurationAdjusted

def test_coordinate_timeline_dynamic_drift():
    print('\n▶️  [STARTING TEST] test_coordinate_timeline_dynamic_drift')
    
    # Assert physical binaries are present
    try:
        subprocess.run(["sqlite3", "-version"], capture_output=True, check=True)
    except Exception as e:
        raise AssertionError(f"CRITICAL FAILURE: sqlite3 CLI binary is missing or not callable: {e}")
        
    # Setup GSA in integration harness
    with IntegrationHarness(required_agents=["gsa"], capabilities=[]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        gsa_url = f"http://127.0.0.1:{gsa_port}/"
        db_file = os.path.join(db_dir, "events.db")
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        # Initialize script with 3 blocks
        blocks = [
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="First text.", duration_sec=3.0),
            ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator", text="Second text.", duration_sec=3.0),
            ScriptBlock(scene_num=1, block_id="s1_b3", speaker="narrator", text="Third text.", duration_sec=3.0)
        ]
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")
        
        # Get duration before adjustment via live GSA HTTP GET
        resp_before = httpx.get(gsa_url)
        assert resp_before.status_code == 200
        state_before = resp_before.json()
        duration_before = float(state_before["otio"]["duration_sec"])
        assert duration_before == 9.0
        
        # Adjust duration of block 1 (increase by 2.0s from 3.0s to 5.0s)
        event_store.append(DurationAdjusted(
            agent="audio", block_id="s1_b1", slot_id="A1:1:s1_b1",
            scene_num=1, voice_role="narrator", scripted_sec=3.0, measured_sec=5.0
        ), "initial_hash")
        
        # Get duration after adjustment via live GSA HTTP GET
        resp_after = httpx.get(gsa_url)
        assert resp_after.status_code == 200
        state_after = resp_after.json()
        duration_after = float(state_after["otio"]["duration_sec"])
        
        # Assert that the total timeline duration increased exactly by 2.0 seconds (SC-08)
        assert duration_after - duration_before == 2.0
        assert duration_after == 11.0
        
        # Verify the start/end coordinates of blocks 2 and 3 are shifted in GSA GET response (SC-08)
        slots_after = state_after["otio"]["slots"]
        
        assert slots_after["A1:1:s1_b1"]["start_sec"] == 0.0
        assert slots_after["A1:1:s1_b1"]["end_sec"] == 5.0
        
        assert slots_after["A1:1:s1_b2"]["start_sec"] == 5.0
        assert slots_after["A1:1:s1_b2"]["end_sec"] == 8.0
        
        assert slots_after["A1:1:s1_b3"]["start_sec"] == 8.0
        assert slots_after["A1:1:s1_b3"]["end_sec"] == 11.0
        
        # Verify database contents using physical sqlite3 CLI command
        res = subprocess.run(["sqlite3", db_file, "SELECT seq, kind FROM events ORDER BY seq"], capture_output=True, text=True, check=True)
        assert "duration_adjusted" in res.stdout
        
        # Assert database-native high precision subtraction using sqlean (Condition 2/3)
        sqlean.extensions.enable_all()
        conn = sqlean.connect(db_file)
        query = '''
            SELECT time_sub(
                time_date(2026, 6, 2, 12, 0, CAST(json_extract(effect_json, '$.measured_sec') AS INTEGER), 0),
                time_date(2026, 6, 2, 12, 0, CAST(json_extract(effect_json, '$.scripted_sec') AS INTEGER), 0)
            )
            FROM events
            WHERE kind = 'duration_adjusted'
        '''
        res_sqlean = conn.execute(query).fetchone()
        conn.close()
        assert res_sqlean[0] == 2 * 1000000000
        
        print("✓ Coordinate Timeline dynamic drift verified.")
