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
    parse_duration, Effect, KIND_TO_MODEL, EffectUnion, CommandExecuted,
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

def test_bdd_full_fleet_teardown_cost_accounting():

    print('\n▶️  [STARTING TEST] test_bdd_full_fleet_teardown_cost_accounting')
    """Given pipeline complete, 2 VMs still active,
    When both VMs are destroyed,
    Then VMDeallocated for both, 0 active VMs, final cost recorded.

    BDD judge evaluates: complete teardown, cost accounting accuracy.
    Intensity: Medium (event store + projections)
    """
    scenario = BddScenario(
        test_name="full_fleet_teardown_cost_accounting",
        given="Pipeline complete, 2 VMs active (tts=1234567, ltx=7654321)",
        when="Both VMs destroyed after pipeline completion",
        then="VMDeallocated emitted for both, 0 active VMs in projection, "
             "budget shows total spent",
    )
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bdd_teardown_")

    store = EventStore(log_dir=tmp)
    store._init_db()

    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(PipelineStarted(agent="operator", output_path=f"{tmp}/final.mp4"), "")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(BudgetSet(agent="operator", budget_usd=10.0), "")

    # Seed physical trace commands to support the VM allocation events
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(CommandExecuted(
        agent="provisioner", command="vastai create instance 101 --image ubuntu:22.04 --disk 20 --raw",
        exit_code=0, stdout_hash="hash101"
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(VMAllocated(
        agent="provisioner", instance_id="1234567", role="tts",
        offer_id="101", worker_url="http://127.0.0.1:9001",
        gpu_type="RTX 4090", cost_per_hour=0.85,
    ), "init")

    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(CommandExecuted(
        agent="provisioner", command="vastai create instance 102 --image ubuntu:22.04 --disk 20 --raw",
        exit_code=0, stdout_hash="hash102"
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(VMAllocated(
        agent="provisioner", instance_id="7654321", role="ltx",
        offer_id="102", worker_url="http://127.0.0.1:9002",
        gpu_type="RTX 4090", cost_per_hour=0.90,
    ), "init")

    # Pipeline complete
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(PipelineComplete(
        agent="assembly", output_path=f"{tmp}/final.mp4", duration_sec=15.0,
    ), "init")

    # Teardown both (with physical command executed events)
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(CommandExecuted(
        agent="provisioner", command="vastai destroy instance 1234567",
        exit_code=0, stdout_hash="destroyed"
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(VMDeallocated(
        agent="provisioner", instance_id="1234567", reason="job_done",
        runtime_sec=15.0, final_cost=0.0035
    ), "init")

    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(CommandExecuted(
        agent="provisioner", command="vastai destroy instance 7654321",
        exit_code=0, stdout_hash="destroyed"
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(VMDeallocated(
        agent="provisioner", instance_id="7654321", reason="job_done",
        runtime_sec=15.0, final_cost=0.0038
    ), "init")

    events = store.replay()

    # VMs projection
    vp = VMs()
    for e in events:
        vp.apply(e.effect)
    active_count = sum(1 for v in vp.active.values() if v.status == "active")
    print('     ├─ [Assert] Checking: active_count == 0, f\"Expected 0 active VMs, got {active_cou...')
    assert active_count == 0, f"Expected 0 active VMs, got {active_count}"

    # State projection
    sp = StateProjection()
    for e in events:
        sp.apply(e.effect)

    # Budget projection
    bp = BudgetProjection()
    for e in events:
        bp.apply(e.effect)

    scenario.evidence = collect_evidence_from_store(
        events,
        projections={
            "active_vms": active_count,
            "total_vms_ever": len(vp.active),
            "pipeline_phase": sp.phase,
            "budget_usd": bp.budget_usd,
            "spent_usd": bp.spent_usd,
        },
    )
    print('     ├─ [BDD Judge] Executing LLM BDD Judge validation...')
    verdict = asyncio.run(run_bdd_judge(scenario, tmp))
    print('     ├─ [Assert] Checking: verdict[\"verdict\"] != \"fail\", f\"BDD judge failed: {verdict[\'reasoning\']}\"')
    assert verdict["verdict"] != "fail", f"BDD judge failed: {verdict['reasoning']}"
    print(f"    ✓ full fleet teardown — verdict: {verdict['verdict']}")


# ===========================================================================
# 51. Real Perplexity Verify Live (Verifies Perplexity API integration)
# ===========================================================================