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


def test_simulation_jobs_projection_dirty_clean_tracking():

    print('\n▶️  [STARTING TEST] test_simulation_jobs_projection_dirty_clean_tracking')
    """Verify dirty/clean block tracking across the job lifecycle.

    A block is dirty when queued and becomes clean when completed.
    Exercises the reconciliation tracking that audio agent relies on.
    Intensity: Medium
    """
    jobs = Jobs()

    slot_id = "A1:1:s1_b1"
    jobs.apply(QueueJob(agent="audio", job_id="j-001", job_type="tts",
                        scene_num=1, block_id="s1_b1", slot_id=slot_id))

    print('     ├─ [Assert] Checking: slot_id in jobs.dirty_blocks')
    assert slot_id in jobs.dirty_blocks
    print('     ├─ [Assert] Checking: not jobs.is_block_clean(slot_id)')
    assert not jobs.is_block_clean(slot_id)

    # Start → still dirty
    jobs.apply(JobStarted(agent="provisioner", job_id="j-001",
                          vm_instance_id="vm-1"))
    print('     ├─ [Assert] Checking: slot_id in jobs.dirty_blocks')
    assert slot_id in jobs.dirty_blocks

    # Complete → block becomes clean
    jobs.apply(JobCompleted(agent="provisioner", job_id="j-001",
                            artifact_uri="/tmp/out.wav", duration_sec=5.0,
                            vm_instance_id="vm-1"))
    print('     ├─ [Assert] Checking: slot_id not in jobs.dirty_blocks')
    assert slot_id not in jobs.dirty_blocks
    print('     ├─ [Assert] Checking: jobs.is_block_clean(slot_id)')
    assert jobs.is_block_clean(slot_id)

    # Requeue → dirty again
    jobs.apply(JobRequeued(agent="audio", job_id="j-001",
                           reason="duration too long"))
    print('     ├─ [Assert] Checking: slot_id in jobs.dirty_blocks')
    assert slot_id in jobs.dirty_blocks
    print('     ├─ [Assert] Checking: not jobs.is_block_clean(slot_id)')
    assert not jobs.is_block_clean(slot_id)

    # Verify requeue count
    print('     ├─ [Assert] Checking: jobs.jobs[\"j-001\"].requeue_count == 1')
    assert jobs.jobs["j-001"].requeue_count == 1

    print("    ✓ dirty/clean block tracking through queue→complete→requeue")


# ===========================================================================
# 32. VMs Projection: Multi-Role Fleet Management
# ===========================================================================