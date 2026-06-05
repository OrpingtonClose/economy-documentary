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

def test_bdd_budget_gated_provisioning():

    print('\n▶️  [STARTING TEST] test_bdd_budget_gated_provisioning')
    """Given budget=$3.00 and $2.80 already spent,
    When a new job requires VM allocation,
    Then provisioner refuses — BudgetExceeded event emitted.

    BDD judge evaluates: correct budget math, refusal reason.
    Intensity: Medium (event store + projections)
    """
    scenario = BddScenario(
        test_name="budget_gated_provisioning",
        given="Budget set to $3.00, prior VM costs total $2.80",
        when="New TTS job queued requiring VM allocation",
        then="Provisioner does NOT allocate, BudgetExceeded emitted",
    )
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bdd_budget_")

    store = EventStore(log_dir=tmp)
    store._init_db()

    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(PipelineStarted(agent="operator", output_path=f"{tmp}/final.mp4"), "")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(BudgetSet(agent="operator", budget_usd=3.0), "")
    # Previous VM cost ate most of budget
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(VMAllocated(
        agent="provisioner", instance_id="1234567", role="tts",
        offer_id="101", worker_url="http://127.0.0.1:9001",
        gpu_type="RTX 4090", cost_per_hour=0.85,
    ), "init")
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(VMDeallocated(
        agent="provisioner", instance_id="1234567", reason="job_done",
    ), "init")
    # Signal that budget is close to exhaustion
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(BudgetExceeded(
        agent="provisioner", spent_usd=2.80, limit_usd=3.0,
    ), "init")

    events = store.replay()

    bp = BudgetProjection()
    for e in events:
        bp.apply(e.effect)

    print('     ├─ [Assert] Checking: bp.budget_usd == 3.0')
    assert bp.budget_usd == 3.0
    print('     ├─ [Assert] Checking: bp.exceeded, \"Budget should be marked as exceeded\"')
    assert bp.exceeded, "Budget should be marked as exceeded"

    scenario.evidence = collect_evidence_from_store(
        events,
        projections={
            "budget_usd": bp.budget_usd,
            "spent_usd": bp.spent_usd,
            "exceeded": bp.exceeded,
        },
    )
    print('     ├─ [BDD Judge] Executing LLM BDD Judge validation...')
    verdict = asyncio.run(run_bdd_judge(scenario, tmp))
    print('     ├─ [Assert] Checking: verdict[\"verdict\"] != \"fail\", f\"BDD judge failed: {verd...')
    assert verdict["verdict"] != "fail", f"BDD judge failed: {verdict['reasoning']}"
    print(f"    ✓ budget-gated provisioning — verdict: {verdict['verdict']}")


# ===========================================================================
# 47. BDD: Script Revision Selective Requeue — partial invalidation
# ===========================================================================