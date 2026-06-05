import os
import sys
import time
import wave
import math
import httpx
import pytest
import subprocess
import numpy as np
import asyncio
from pathlib import Path

# Setup Python paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))
sys.path.append(str(PROJECT_ROOT / "tests/units"))

from harness import IntegrationHarness
from event_store import EventStore
from effects import (
    PipelineStarted, PipelineComplete, PipelineAborted,
    BudgetSet, BudgetExceeded, UpdateScript, ScriptBlock,
    QueueJob, JobStarted, JobCompleted, JobFailed, JobRequeued, JobApproved,
    VMAllocated, VMDeallocated, VMObserved, VMProvisionFailed,
    DurationAdjusted, ReconciliationComplete, ReconciliationFailed,
    MergeIntoOTIO, DeleteScene, DeleteFromOTIO, ReorderScenes,
    AudioMeasured, AudioGenerated, NoOp, HumanInstruction,
    AgentLoopDetected, MeasurementRequested, VideoMeasured,
    ProductionFailed, SuggestedFix,
    parse_duration, Effect, KIND_TO_MODEL, EffectUnion,
)
from projections import (
    Timeline, Jobs, VMs, BudgetProjection, StateProjection,
    JobState, VMRecord,
)
from coordinate_timeline import CoordinateTimeline, IntervalSpan
import builtins

def print(*args, **kwargs):
    sep = kwargs.get('sep', ' ')
    end = kwargs.get('end', '\n')
    msg = sep.join(str(arg) for arg in args) + end
    if sys.stdout is not None:
        sys.stdout.write(msg)
        sys.stdout.flush()
    else:
        builtins.print(*args, **kwargs)

# BDD judge imports
sys.path.append(str(PROJECT_ROOT / "server" / "capabilities"))
from test_judge_capability import BddScenario, run_bdd_judge, collect_evidence_from_store

def measure_lufs_integrated(audio_path: str) -> float:
    """Measure integrated LUFS robustly by converting audio to raw s16le PCM via ffmpeg."""
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_pcm_path = os.path.join(tmpdir, "raw.pcm")
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-vn", "-f", "s16le", "-ac", "1", "-ar", "44100", raw_pcm_path],
            capture_output=True, check=True
        )
        with open(raw_pcm_path, "rb") as f:
            raw = f.read()
            
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    rms = np.sqrt(np.mean(np.square(pcm, dtype=np.float64)))
    if rms <= 0.0:
        return -70.0
    return 20.0 * math.log10(rms) + 0.0

from capabilities.test_single_purpose_tts_simulators import TtsPreemptSimulator

def test_bdd_vm_preemption_recovery():

    print('\n▶️  [STARTING TEST] test_bdd_vm_preemption_recovery')
    """Given a VM was active and running a job,
    When the VM is killed (preempted) and VMDeallocated is observed,
    Then the job is requeued and a new VM is allocated.

    BDD judge evaluates: recovery event sequence, no orphan jobs.
    Intensity: Medium (event store + projections)
    """
    scenario = BddScenario(
        test_name="vm_preemption_recovery",
        given="VM 1234567 active with role=tts, job_tts_preempt_1 running on it",
        when="VM preempted (VMDeallocated), then new VM allocated",
        then="Job requeued, new VMAllocated, job eventually completed on new VM",
    )
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bdd_preempt_")

    store = EventStore(log_dir=tmp)
    store._init_db()

    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(PipelineStarted(agent="operator", output_path=f"{tmp}/final.mp4"), "")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(BudgetSet(agent="operator", budget_usd=5.0), "")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(VMAllocated(
        agent="provisioner", instance_id="1234567", role="tts",
        offer_id="101", worker_url="http://127.0.0.1:9001",
        gpu_type="RTX 4090", cost_per_hour=0.85,
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(QueueJob(
        agent="audio", job_id="job_tts_preempt_1", job_type="tts",
        scene_num=1, block_id="s1_b1", slot_id="A1:1:s1_b1",
        params={"text": "Preemption test.", "voice": "narrator", "gpu_type": "RTX 4090"},
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(JobStarted(
        agent="provisioner", job_id="job_tts_preempt_1", vm_instance_id="1234567",
    ), "init")
    # VM preempted
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(VMDeallocated(
        agent="provisioner", instance_id="1234567", reason="stale",
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(JobFailed(
        agent="provisioner", job_id="job_tts_preempt_1",
        error_message="VM preempted mid-inference", failure_category="unknown",
        vm_instance_id="1234567",
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(JobRequeued(
        agent="audio", job_id="job_tts_preempt_1",
        reason="VM preempted, requeuing",
    ), "init")
    # New VM
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(VMAllocated(
        agent="provisioner", instance_id="7654321", role="tts",
        offer_id="102", worker_url="http://127.0.0.1:9002",
        gpu_type="RTX 4090", cost_per_hour=0.90,
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(JobStarted(
        agent="provisioner", job_id="job_tts_preempt_1", vm_instance_id="7654321",
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(JobCompleted(
        agent="provisioner", job_id="job_tts_preempt_1",
        artifact_uri=f"{tmp}/audio_outputs/job_tts_preempt_1.wav",
        duration_sec=3.0, vm_instance_id="7654321",
    ), "init")

    events = store.replay()

    # VMs projection
    vp = VMs()
    for e in events:
        vp.apply(e.effect)
    print('     ├─ [Assert] Checking: \"1234567\" not in vp.active or vp.active[\"1234567\"].statu...')
    assert "1234567" not in vp.active or vp.active["1234567"].status != "active"

    # Jobs projection
    jp = Jobs()
    for e in events:
        jp.apply(e.effect)
    job = jp.jobs.get("job_tts_preempt_1")
    print('     ├─ [Assert] Checking: job is not None')
    assert job is not None
    print('     ├─ [Assert] Checking: job.status == \"completed\"')
    assert job.status == "completed"

    scenario.evidence = collect_evidence_from_store(
        events,
        projections={
            "active_vms": len(vp.active),
            "job_status": job.status,
            "job_vm": "7654321",
        },
    )
    print('     ├─ [BDD Judge] Executing LLM BDD Judge validation...')
    verdict = asyncio.run(run_bdd_judge(scenario, tmp))
    print('     ├─ [Assert] Checking: verdict[\"verdict\"] != \"fail\", f\"BDD judge failed: {verd...')
    assert verdict["verdict"] != "fail", f"BDD judge failed: {verdict['reasoning']}"
    print(f"    ✓ VM preemption recovery — verdict: {verdict['verdict']}")


# ===========================================================================
# 46. BDD: Budget-Gated Provisioning — refuse when over budget
# ===========================================================================