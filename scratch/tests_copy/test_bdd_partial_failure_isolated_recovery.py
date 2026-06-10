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

from capabilities.test_single_purpose_tts_simulators import TtsJob1Simulator

def test_bdd_partial_failure_isolated_recovery():

    print('\n▶️  [STARTING TEST] test_bdd_partial_failure_isolated_recovery')
    """Given 4 scenes, scene 3 TTS failed,
    When audio retries scene 3 and video proceeds for scenes 1,2,4,
    Then scene 3 eventually completes, other scenes unaffected.

    BDD judge evaluates: failure isolation, eventual convergence.
    Intensity: Medium (event store + projections)
    """
    scenario = BddScenario(
        test_name="partial_failure_isolated_recovery",
        given="4 scenes queued; scene 3 TTS job failed, others completed",
        when="Scene 3 job requeued and eventually completed",
        then="All 4 jobs in 'completed' status, scene 3 has attempts > 1",
    )
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bdd_partial_")

    store = EventStore(log_dir=tmp)
    store._init_db()

    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(PipelineStarted(agent="operator", output_path=f"{tmp}/final.mp4"), "")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(VMAllocated(
        agent="provisioner", instance_id="1234567", role="tts",
        offer_id="101", worker_url="http://127.0.0.1:9001",
        gpu_type="RTX 4090", cost_per_hour=0.85,
    ), "init")

    # Scenes 1,2,4 complete normally
    for scene_num in [1, 2, 4]:
        jid = f"job_tts_scene{scene_num}"
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(QueueJob(
            agent="audio", job_id=jid, job_type="tts",
            scene_num=scene_num, block_id=f"s{scene_num}_b1",
            slot_id=f"A1:{scene_num}:s{scene_num}_b1",
            params={"text": f"Scene {scene_num} narration.", "voice": "narrator"},
        ), "init")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(JobStarted(agent="provisioner", job_id=jid, vm_instance_id="1234567"), "init")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(JobCompleted(
            agent="provisioner", job_id=jid,
            artifact_uri=f"{tmp}/{jid}.wav", duration_sec=3.0,
            vm_instance_id="1234567",
        ), "init")

    # Scene 3: fails, then retries, then completes
    jid3 = "job_tts_scene3"
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(QueueJob(
        agent="audio", job_id=jid3, job_type="tts",
        scene_num=3, block_id="s3_b1", slot_id="A1:3:s3_b1",
        params={"text": "Scene 3 narration.", "voice": "narrator"},
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(JobStarted(agent="provisioner", job_id=jid3, vm_instance_id="1234567"), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(JobFailed(
        agent="provisioner", job_id=jid3,
        error_message="CUDA OOM", failure_category="oom",
        vm_instance_id="1234567",
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(JobRequeued(agent="audio", job_id=jid3, reason="Retry after CUDA OOM"), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(JobStarted(agent="provisioner", job_id=jid3, vm_instance_id="1234567"), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(JobCompleted(
        agent="provisioner", job_id=jid3,
        artifact_uri=f"{tmp}/{jid3}.wav", duration_sec=3.0,
        vm_instance_id="1234567",
    ), "init")

    events = store.replay()

    jp = Jobs()
    for e in events:
        jp.apply(e.effect)

    # All 4 complete
    for sn in [1, 2, 3, 4]:
        jid = f"job_tts_scene{sn}"
        print('     ├─ [Assert] Checking: jp.jobs[jid].status == \"completed\", f\"{jid} not completed...')
        assert jp.jobs[jid].status == "completed", f"{jid} not completed"

    # Scene 3 had retry
    print('     ├─ [Assert] Checking: jp.jobs[\"job_tts_scene3\"].attempts > 1, \"Scene 3 should h...')
    assert jp.jobs["job_tts_scene3"].attempts > 1, "Scene 3 should have multiple attempts"

    scenario.evidence = collect_evidence_from_store(
        events,
        projections={
            "all_completed": all(j.status == "completed" for j in jp.jobs.values()),
            "scene3_attempts": jp.jobs["job_tts_scene3"].attempts,
            "total_jobs": len(jp.jobs),
        },
    )
    print('     ├─ [BDD Judge] Executing LLM BDD Judge validation...')
    verdict = asyncio.run(run_bdd_judge(scenario, tmp))
    print('     ├─ [Assert] Checking: verdict[\"verdict\"] != \"fail\", f\"BDD judge failed: {verd...')
    assert verdict["verdict"] != "fail", f"BDD judge failed: {verdict['reasoning']}"
    print(f"    ✓ partial failure isolated recovery — verdict: {verdict['verdict']}")


# ===========================================================================
# 50. BDD: Full Fleet Teardown — cost accounting, no orphans
# ===========================================================================