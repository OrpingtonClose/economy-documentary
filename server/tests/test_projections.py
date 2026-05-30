import pytest
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
    assert timeline.all_slots_filled()


def test_jobs_projection():
    run_id = "run-2"
    jobs = Jobs()

    # Queue job
    event1 = QueueJob(
        run_id=run_id,
        agent="audio",
        job_id="job1",
        job_type="tts",
        scene_num=1,
        block_id="A1:1:b1",
        slot_id="A1:1:b1",
        params={"voice": "narrator"}
    )
    jobs.apply(event1)
    assert len(jobs.jobs) == 1
    assert jobs.jobs["job1"].status == "pending"
    assert jobs.block_attempts["A1:1:b1"] == 1

    # Start job
    event2 = JobStarted(
        run_id=run_id,
        agent="provisioner",
        job_id="job1",
        vm_instance_id="vm1"
    )
    jobs.apply(event2)
    assert jobs.jobs["job1"].status == "running"
    assert jobs.jobs["job1"].vm_instance_id == "vm1"

    # Complete job
    event3 = JobCompleted(
        run_id=run_id,
        agent="provisioner",
        job_id="job1",
        artifact_uri="http://out.wav",
        duration_sec=5.0,
        vm_instance_id="vm1"
    )
    jobs.apply(event3)
    assert jobs.jobs["job1"].status == "completed"
    assert jobs.jobs["job1"].artifact_uri == "http://out.wav"


def test_vms_projection():
    run_id = "run-3"
    vms = VMs()

    # VM allocated
    event1 = VMAllocated(
        run_id=run_id,
        agent="provisioner",
        instance_id="vm1",
        role="tts",
        offer_id="offer123",
        worker_url="http://1.2.3.4:9000",
        gpu_type="RTX 4090",
        cost_per_hour=0.45
    )
    vms.apply(event1)
    assert len(vms.vms) == 1
    assert vms.vms["vm1"].status == "active"
    assert vms.vms["vm1"].hourly_rate_usd == 0.45

    # VM deallocated
    event2 = VMDeallocated(
        run_id=run_id,
        agent="provisioner",
        instance_id="vm1",
        reason="job_done",
        final_cost=0.03,
        runtime_sec=240.0
    )
    vms.apply(event2)
    assert vms.vms["vm1"].status == "destroyed"


def test_budget_projection():
    run_id = "run-4"
    budget = BudgetProjection()

    # Set budget
    event1 = BudgetSet(
        run_id=run_id,
        agent="scenario",
        budget_usd=5.0,
        reason="run_start"
    )
    budget.apply(event1)
    assert budget.budget_cap_usd == 5.0
    assert not budget.exceeded

    # Accrue cost
    event2 = VMDeallocated(
        run_id=run_id,
        agent="provisioner",
        instance_id="vm1",
        reason="job_done",
        final_cost=6.0,
        runtime_sec=240.0
    )
    budget.apply(event2)
    assert budget.spent_usd == 6.0
    assert budget.exceeded
    assert budget.remaining_usd() == -1.0


def test_state_projection():
    run_id = "run-5"
    state = StateProjection()

    # Reconciliation complete transitions phase
    event1 = ReconciliationComplete(
        run_id=run_id,
        agent="audio",
        blocks_total=1,
        blocks_passed=1,
        blocks_failed=0,
        worst_delta_sec=0.1,
        total_measured_sec=9.5
    )
    state.apply(event1)
    assert state.current_phase == "audio_reconcile"
    assert len(state.phase_history) == 1
    assert state.phase_history[0].to_phase == "audio_reconcile"
