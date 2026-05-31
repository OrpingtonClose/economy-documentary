"import pytest
from projections import Timeline, Jobs, VMs, BudgetProjection, StateProjection
from effects import UpdateScript, ScriptBlock, QueueJob, JobStarted, JobCompleted, VMAllocated, VMDeallocated, BudgetSet, ReconciliationComplete, MergeIntoOTIO, DurationAdjusted
from uuid_utils import uuid7


def test_timeline_projection():
    run_id = "run-1"
    timeline = Timeline()

    # 1. Update script -> creates slots
    event1 = UpdateScript(
        run_id=run_id,
        agent="scenario",
        blocks=[
            ScriptBlock(
                scene_num=1,
                block_id="b1",
                speaker="narrator",
                text="Narration 1",
                duration_sec=10.0
            )
        ]
    )
    timeline.apply(event1)
    assert len(timeline.slots) == 1
    assert "A1:1:b1" in timeline.slots
    assert timeline.slots["A1:1:b1"]["status"] == "scripted"
    assert timeline.slots["A1:1:b1"]["scripted_sec"] == 10.0

    # 2. Duration adjusted -> measured status
    event2 = DurationAdjusted(
        run_id=run_id,
        agent="audio",
        block_id="A1:1:b1",
        slot_id="A1:1:b1",
        scene_num=1,
        voice_role="narrator",
        scripted_sec=10.0,
        measured_sec=9.5
    )
    timeline.apply(event2)
    assert timeline.slots["A1:1:b1"]["status"] == "measured"
    assert timeline.slots["A1:1:b1"]["measured_sec"] == 9.5

    # 3. Merge into OTIO -> delivered status
    event3 = MergeIntoOTIO(
        run_id=run_id,
        agent="audio",
        job_id="job_id_1",
        block_id="A1:1:b1",
        scene_num=1,
        slot_id="A1:1:b1",
        artifact_uri="http://media.wav",
        track_name="A1_Narration",
        duration_sec=9.5
    )
    timeline.apply(event3)
    assert timeline.slots["A1:1:b1"]["status"] == "delivered"
    assert timeline.slots["A1:1:b1"]["artifact_uri"] == "http://media.wav"
    assert timeline.all_slots_fi
<truncated 3262 bytes>