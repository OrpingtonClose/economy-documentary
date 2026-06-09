import os
import sys
import tempfile
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from event_store import EventStore
from effects import PipelineStarted, UpdateScript, ScriptBlock, DurationAdjusted
from projections import Timeline
from coordinate_timeline import CoordinateTimeline
import opentimelineio as otio

def test_coordinate_timeline_dynamic_drift():
    print('\n▶️  [STARTING TEST] test_coordinate_timeline_dynamic_drift')
    # Guard: Ensure sqlite3 binary exists (Condition 2: live shell command and physical binary)
    try:
        subprocess.run(["sqlite3", "-version"], capture_output=True, check=True)
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: sqlite3 CLI binary is missing or not callable: {e}")
    
    with tempfile.TemporaryDirectory() as db_dir:
        # Initialize physical SQLite database (Condition 2: live boundary interaction)
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        db_file = os.path.join(db_dir, "events.db")
        assert os.path.exists(db_file), "CRITICAL FAILURE: physical events database was not created!"
        
        # Initialize script with 3 blocks
        blocks = [
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="First text.", duration_sec=3.0),
            ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator", text="Second text.", duration_sec=3.0),
            ScriptBlock(scene_num=1, block_id="s1_b3", speaker="narrator", text="Third text.", duration_sec=3.0)
        ]
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")
        
        # Adjust duration of block 1 (increase by 2.0s)
        event_store.append(DurationAdjusted(
            agent="audio", block_id="s1_b1", slot_id="A1:1:s1_b1",
            scene_num=1, voice_role="narrator", scripted_sec=3.0, measured_sec=5.0
        ), "initial_hash")
        
        # Verify database contents using physical sqlite3 CLI command (Condition 2: live shell command)
        res = subprocess.run(["sqlite3", db_file, "SELECT seq, kind FROM events ORDER BY seq"], capture_output=True, text=True, check=True)
        assert "duration_adjusted" in res.stdout
        
        # Reconstruct projections from sequence 0 directly via the physical DB
        timeline = Timeline()
        timeline.tick(event_store)
        
        # Verify total duration increased by exactly 2.0s (to 11.0s total)
        duration = timeline.get_timeline_duration_sec()
        assert duration == 11.0
        
        # Direct check on CoordinateTimeline projection to verify start/end coordinates of blocks 2 and 3 (SC-08)
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
