import os
import sys
import pytest
import shutil
from pathlib import Path

# Append server path to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from effects import (
    UpdateScript,
    ScriptBlock,
    MergeIntoOTIO,
    DurationAdjusted,
    PipelineStarted,
)
from event_store import EventStore
from coordinate_timeline import CoordinateTimeline, IntervalSpan

@pytest.fixture
def clean_event_store():
    db_dir = "/tmp/coordinate-pipeline-test"
    try:
        shutil.rmtree(db_dir)
    except Exception:
        _err = True
    os.makedirs(db_dir, exist_ok=True)
    store = EventStore(log_dir=db_dir)
    yield store
    try:
        shutil.rmtree(db_dir)
    except Exception:
        _err = True

def test_range_overlap_exclusion():
    timeline = CoordinateTimeline()
    
    # 1. Seed screenplay blocks
    block_1 = ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="Block 1 Text", duration_sec=3.0)
    block_2 = ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator", text="Block 2 Text", duration_sec=3.0)
    script_event = UpdateScript(agent="scenario", blocks=[block_1, block_2])
    timeline.apply(script_event)
    
    # 2. Merge block 1 successfully
    merge_1 = MergeIntoOTIO(
        agent="assembly",
        job_id="job_tts_1",
        block_id="s1_b1",
        scene_num=1,
        slot_id="A1:1:s1_b1",
        artifact_uri="/tmp/audio/s1_b1.wav",
        track_name="A1_Narration",
        duration_sec=3.0,
        start_sec=0.0
    )
    timeline.apply(merge_1)
    
    # 3. Attempt to merge overlapping block 3 (collides with block 1 range 0.0 - 3.0)
    merge_overlap = MergeIntoOTIO(
        agent="assembly",
        job_id="job_tts_2",
        block_id="s1_b3",
        scene_num=1,
        slot_id="A1:1:s1_b3",
        artifact_uri="/tmp/audio/s1_b3.wav",
        track_name="A1_Narration",
        duration_sec=3.0,
        start_sec=1.5
    )
    
    with pytest.raises(ValueError) as exc:
        timeline.apply(merge_overlap)
        
    assert "Collision on track 'A1_Narration'" in str(exc.value)


def test_dynamic_downstream_shift():
    timeline = CoordinateTimeline()
    
    # 1. Seed two screenplay blocks
    block_1 = ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="First phrase.", duration_sec=3.0)
    block_2 = ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator", text="Second phrase.", duration_sec=3.0)
    script_event = UpdateScript(agent="scenario", blocks=[block_1, block_2])
    timeline.apply(script_event)
    
    # 2. Merge both clips
    merge_1 = MergeIntoOTIO(
        agent="assembly",
        job_id="job_tts_1",
        block_id="s1_b1",
        scene_num=1,
        slot_id="A1:1:s1_b1",
        artifact_uri="/tmp/audio/s1_b1.wav",
        track_name="A1_Narration",
        duration_sec=3.0,
        start_sec=0.0
    )
    timeline.apply(merge_1)
    
    merge_2 = MergeIntoOTIO(
        agent="assembly",
        job_id="job_tts_2",
        block_id="s1_b2",
        scene_num=1,
        slot_id="A1:1:s1_b2",
        artifact_uri="/tmp/audio/s1_b2.wav",
        track_name="A1_Narration",
        duration_sec=3.0,
        start_sec=3.0
    )
    timeline.apply(merge_2)
    
    # Assert initial positions
    clips = timeline.clips["A1_Narration"]
    assert len(clips) == 2
    
    clip_1 = next(c for c in clips if c.scenario_id == "s1_b1")
    clip_2 = next(c for c in clips if c.scenario_id == "s1_b2")
    
    assert clip_1.span.start_sec == 0.0
    assert clip_1.span.end_sec == 3.0
    assert clip_2.span.start_sec == 3.0
    assert clip_2.span.end_sec == 6.0
    
    # 3. Adjust block 1 duration to 3.5s (should trigger cascade shift on block 2)
    adjust_event = DurationAdjusted(
        agent="audio",
        block_id="A1:1:s1_b1",
        slot_id="A1:1:s1_b1",
        scene_num=1,
        voice_role="narrator",
        scripted_sec=3.0,
        measured_sec=3.5
    )
    timeline.apply(adjust_event)
    
    # Assert shifted positions
    assert clip_1.span.start_sec == 0.0
    assert clip_1.span.end_sec == 3.5
    assert clip_2.span.start_sec == 3.5
    assert clip_2.span.end_sec == 6.5


def test_sqlean_high_precision_time():
    timeline = CoordinateTimeline()
    
    # Query duration of 3.23s starting at 12.0s
    ns_diff = timeline.query_sqlean_timespan(12.0, 3.23)
    
    # Should calculate exactly 3,230,000,000 nanoseconds
    assert ns_diff == 3230000000


def test_point_in_time_event_replay(clean_event_store):
    # 1. Write events sequentially to EventStore
    clean_event_store.append(PipelineStarted(agent="operator"), "initial_hash")
    
    block_1 = ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="Block 1", duration_sec=3.0)
    clean_event_store.append(UpdateScript(agent="scenario", blocks=[block_1]), "initial_hash")
    
    merge_event = MergeIntoOTIO(
        agent="assembly",
        job_id="job_tts_1",
        block_id="s1_b1",
        scene_num=1,
        slot_id="A1:1:s1_b1",
        artifact_uri="/tmp/audio/s1_b1.wav",
        track_name="A1_Narration",
        duration_sec=3.0,
        start_sec=0.0
    )
    clean_event_store.append(merge_event, "initial_hash")
    
    # 2. Time travel: Replay only up to sequence 2 (before the merge clip event)
    timeline_seq2 = CoordinateTimeline()
    records_seq2 = clean_event_store.read_since(0)
    
    for r in records_seq2:
        if r.seq <= 2:
            timeline_seq2.apply(r.effect)
            
    # Assert no clips are merged yet
    assert len(timeline_seq2.clips["A1_Narration"]) == 0
    
    # 3. Full replay to current state (up to seq 3)
    timeline_seq3 = CoordinateTimeline()
    for r in records_seq2:
        if r.seq <= 3:
            timeline_seq3.apply(r.effect)
            
    # Assert clip is now merged
    assert len(timeline_seq3.clips["A1_Narration"]) == 1
    assert timeline_seq3.clips["A1_Narration"][0].scenario_id == "s1_b1"
