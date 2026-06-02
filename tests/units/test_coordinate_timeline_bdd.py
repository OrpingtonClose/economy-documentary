import os
import sys
import pytest
import shutil
from pathlib import Path
from pytest_bdd import scenarios, given, when, then, parsers

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

# Load scenarios
scenarios('features/coordinate_timeline.feature')

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

# --- Scenario 1, 2, 3 shared state ---
class TimelineTestContext:
    def __init__(self):
        self.timeline = None
        self.last_exception = None
        self.query_result = None
        self.event_store = None
        self.replayed_timeline = None

@pytest.fixture
def ctx():
    return TimelineTestContext()

@given("a clean CoordinateTimeline projection")
def step_clean_timeline(ctx):
    ctx.timeline = CoordinateTimeline()

@given("we seed two screenplay blocks of duration 3.0 seconds")
def step_seed_blocks(ctx):
    block_1 = ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="Block 1 Text", duration_sec=3.0)
    block_2 = ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator", text="Block 2 Text", duration_sec=3.0)
    script_event = UpdateScript(agent="scenario", blocks=[block_1, block_2])
    ctx.timeline.apply(script_event)

@given("we merge block 1 at offset 0.0 seconds successfully")
def step_merge_block_1(ctx):
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
    ctx.timeline.apply(merge_1)

@when("we attempt to merge an overlapping block 3 at offset 1.5 seconds")
def step_merge_overlapping(ctx):
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
    try:
        ctx.timeline.apply(merge_overlap)
    except ValueError as exc:
        ctx.last_exception = exc

@then('it should raise a ValueError with "Collision on track"')
def step_assert_collision_error(ctx):
    assert ctx.last_exception is not None
    assert "Collision on track 'A1_Narration'" in str(ctx.last_exception)


# --- Scenario 2 steps ---
@given("we merge block 2 at offset 3.0 seconds successfully")
def step_merge_block_2(ctx):
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
    ctx.timeline.apply(merge_2)

@when(parsers.parse("we adjust block 1 duration to {duration:f} seconds"))
def step_adjust_block_1_duration(ctx, duration):
    adjust_event = DurationAdjusted(
        agent="audio",
        block_id="A1:1:s1_b1",
        slot_id="A1:1:s1_b1",
        scene_num=1,
        voice_role="narrator",
        scripted_sec=3.0,
        measured_sec=duration
    )
    ctx.timeline.apply(adjust_event)

@then(parsers.parse("block 1 span should be {start:f} to {end:f} seconds"))
def step_assert_block_1_span(ctx, start, end):
    clips = ctx.timeline.clips["A1_Narration"]
    clip_1 = next(c for c in clips if c.scenario_id == "s1_b1")
    assert clip_1.span.start_sec == start
    assert clip_1.span.end_sec == end

@then(parsers.parse("block 2 span should be shifted to {start:f} to {end:f} seconds"))
def step_assert_block_2_span(ctx, start, end):
    clips = ctx.timeline.clips["A1_Narration"]
    clip_2 = next(c for c in clips if c.scenario_id == "s1_b2")
    assert clip_2.span.start_sec == start
    assert clip_2.span.end_sec == end


# --- Scenario 3 steps ---
@when("we query the duration of a 3.23s span starting at 12.0s using sqlean-time")
def step_query_sqlean_duration(ctx):
    ctx.query_result = ctx.timeline.query_sqlean_timespan(12.0, 3.23)

@then("it should return exactly 3230000000 nanoseconds")
def step_assert_nanoseconds(ctx):
    assert ctx.query_result == 3230000000


# --- Scenario 4 steps ---
@given("a clean EventStore")
def step_clean_event_store(ctx, clean_event_store):
    ctx.event_store = clean_event_store
    ctx.replayed_timeline = CoordinateTimeline()

@given("we append a PipelineStarted event")
def step_append_pipeline_started(ctx):
    ctx.event_store.append(PipelineStarted(agent="operator"), "initial_hash")

@given("we append an UpdateScript event with block 1 duration 3.0s")
def step_append_update_script(ctx):
    block_1 = ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="Block 1", duration_sec=3.0)
    ctx.event_store.append(UpdateScript(agent="scenario", blocks=[block_1]), "initial_hash")

@given("we append a MergeIntoOTIO event for block 1 at 0.0s")
def step_append_merge_event(ctx):
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
    ctx.event_store.append(merge_event, "initial_hash")

@when("we replay events up to sequence 2")
def step_replay_to_seq_2(ctx):
    ctx.replayed_timeline = CoordinateTimeline()
    records = ctx.event_store.read_since(0)
    for r in records:
        if r.seq <= 2:
            ctx.replayed_timeline.apply(r.effect)

@then("the timeline should contain 0 clips")
def step_assert_zero_clips(ctx):
    assert len(ctx.replayed_timeline.clips["A1_Narration"]) == 0

@when("we replay events up to sequence 3")
def step_replay_to_seq_3(ctx):
    ctx.replayed_timeline = CoordinateTimeline()
    records = ctx.event_store.read_since(0)
    for r in records:
        if r.seq <= 3:
            ctx.replayed_timeline.apply(r.effect)

@then("the timeline should contain 1 clip for block 1")
def step_assert_one_clip_block_1(ctx):
    assert len(ctx.replayed_timeline.clips["A1_Narration"]) == 1
    assert ctx.replayed_timeline.clips["A1_Narration"][0].scenario_id == "s1_b1"


# --- Scenario 5 steps (Isolated tracks) ---
@given(parsers.parse('we merge block 1 on track "{track_name}" at offset {offset:f} seconds successfully'))
def step_merge_block_1_track(ctx, track_name, offset):
    merge_1 = MergeIntoOTIO(
        agent="assembly",
        job_id="job_tts_1",
        block_id="s1_b1",
        scene_num=1,
        slot_id=f"A1:1:s1_b1",
        artifact_uri="/tmp/audio/s1_b1.wav",
        track_name=track_name,
        duration_sec=3.0,
        start_sec=offset
    )
    ctx.timeline.apply(merge_1)

@when(parsers.parse('we merge block 2 on track "{track_name}" at offset {offset:f} seconds successfully'))
def step_merge_block_2_track(ctx, track_name, offset):
    merge_2 = MergeIntoOTIO(
        agent="assembly",
        job_id="job_video_1",
        block_id="s1_b2",
        scene_num=1,
        slot_id=f"V1:1:s1_b2",
        artifact_uri="/tmp/video/s1_b2.mp4",
        track_name=track_name,
        duration_sec=3.0,
        start_sec=offset
    )
    ctx.timeline.apply(merge_2)

@then("both track timelines should contain their respective clips")
def step_assert_track_contents(ctx):
    assert len(ctx.timeline.clips["A1_Narration"]) == 1
    assert len(ctx.timeline.clips["V1_Video"]) == 1
    assert ctx.timeline.clips["A1_Narration"][0].scenario_id == "s1_b1"
    assert ctx.timeline.clips["V1_Video"][0].scenario_id == "s1_b2"


# --- Scenario 6 steps (Recursive shift propagation) ---
@given("we seed three screenplay blocks of duration 3.0 seconds")
def step_seed_three_blocks(ctx):
    block_1 = ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="Block 1 Text", duration_sec=3.0)
    block_2 = ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator", text="Block 2 Text", duration_sec=3.0)
    block_3 = ScriptBlock(scene_num=1, block_id="s1_b3", speaker="narrator", text="Block 3 Text", duration_sec=3.0)
    script_event = UpdateScript(agent="scenario", blocks=[block_1, block_2, block_3])
    ctx.timeline.apply(script_event)

@given("we merge block 3 at offset 6.0 seconds successfully")
def step_merge_block_3(ctx):
    merge_3 = MergeIntoOTIO(
        agent="assembly",
        job_id="job_tts_3",
        block_id="s1_b3",
        scene_num=1,
        slot_id="A1:1:s1_b3",
        artifact_uri="/tmp/audio/s1_b3.wav",
        track_name="A1_Narration",
        duration_sec=3.0,
        start_sec=6.0
    )
    ctx.timeline.apply(merge_3)

@then("block 3 span should be shifted to 7.0 to 10.0 seconds")
def step_assert_block_3_span(ctx):
    clips = ctx.timeline.clips["A1_Narration"]
    clip_3 = next(c for c in clips if c.scenario_id == "s1_b3")
    assert clip_3.span.start_sec == 7.0
    assert clip_3.span.end_sec == 10.0

