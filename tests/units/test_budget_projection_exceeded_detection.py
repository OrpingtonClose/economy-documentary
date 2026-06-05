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


def test_budget_projection_exceeded_detection():

    print('\n▶️  [STARTING TEST] test_budget_projection_exceeded_detection')
    """Set budget, accumulate VM costs, verify exceeded flag triggers.

    Exercises the BudgetProjection's cost accumulation and threshold detection.
    Intensity: Medium
    """
    budget = BudgetProjection()

    # Set a $2.00 budget
    budget.apply(BudgetSet(agent="orchestrator", budget_usd=2.0))
    print('     ├─ [Assert] Checking: budget.budget_cap_usd == 2.0')
    assert budget.budget_cap_usd == 2.0
    print('     ├─ [Assert] Checking: budget.remaining_usd() == 2.0')
    assert budget.remaining_usd() == 2.0
    print('     ├─ [Assert] Checking: not budget.exceeded')
    assert not budget.exceeded

    # First VM costs $0.80
    budget.apply(VMDeallocated(agent="provisioner", instance_id="vm-1",
                               reason="job_done", final_cost=0.80))
    print('     ├─ [Assert] Checking: abs(budget.spent_usd - 0.80) < 0.01')
    assert abs(budget.spent_usd - 0.80) < 0.01
    print('     ├─ [Assert] Checking: abs(budget.remaining_usd() - 1.20) < 0.01')
    assert abs(budget.remaining_usd() - 1.20) < 0.01
    print('     ├─ [Assert] Checking: not budget.exceeded')
    assert not budget.exceeded

    # Second VM costs $0.90 → total $1.70, still under
    budget.apply(VMDeallocated(agent="provisioner", instance_id="vm-2",
                               reason="job_done", final_cost=0.90))
    print('     ├─ [Assert] Checking: abs(budget.spent_usd - 1.70) < 0.01')
    assert abs(budget.spent_usd - 1.70) < 0.01
    print('     ├─ [Assert] Checking: not budget.exceeded')
    assert not budget.exceeded

    # Third VM costs $0.50 → total $2.20, EXCEEDED
    budget.apply(VMDeallocated(agent="provisioner", instance_id="vm-3",
                               reason="job_done", final_cost=0.50))
    print('     ├─ [Assert] Checking: budget.exceeded')
    assert budget.exceeded
    print('     ├─ [Assert] Checking: budget.exceeded_at is not None')
    assert budget.exceeded_at is not None
    print('     ├─ [Assert] Checking: abs(budget.spent_usd - 2.20) < 0.01')
    assert abs(budget.spent_usd - 2.20) < 0.01
    print('     ├─ [Assert] Checking: budget.remaining_usd() < 0')
    assert budget.remaining_usd() < 0

    # Verify per-VM cost ledger
    print('     ├─ [Assert] Checking: len(budget.vm_costs) == 3')
    assert len(budget.vm_costs) == 3

    # Summary should say EXCEEDED
    print('     ├─ [Assert] Checking: \"EXCEEDED\" in budget.summary()')
    assert "EXCEEDED" in budget.summary()

    print("    ✓ budget $2.00, spent $2.20 → exceeded flag + per-VM ledger")


# ===========================================================================
# 34. StateProjection: Full Phase Machine
# ===========================================================================