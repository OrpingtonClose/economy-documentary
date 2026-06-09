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


def test_simulation_jobs_projection_full_lifecycle():

    print('\n▶️  [STARTING TEST] test_simulation_jobs_projection_full_lifecycle')
    """Push 3 jobs through queue→start→complete, verify all status transitions.

    Exercises the Jobs projection's state machine for the happy path.
    Intensity: Medium-Heavy
    """
    jobs = Jobs()

    # Queue 3 jobs (2 TTS, 1 LTX)
    job_defs = [
        ("j-001", "tts", "A1:1:s1_b1"),
        ("j-002", "tts", "A1:1:s1_b2"),
        ("j-003", "ltx", "V1:1:s1_b1"),
    ]
    for job_id, job_type, slot_id in job_defs:
        jobs.apply(QueueJob(agent="audio", job_id=job_id, job_type=job_type,
                            scene_num=1, block_id="b1", slot_id=slot_id))

    print('     ├─ [Assert] Checking: len(jobs.jobs) == 3')
    assert len(jobs.jobs) == 3
    print('     ├─ [Assert] Checking: jobs.has_pending_or_running_jobs()')
    assert jobs.has_pending_or_running_jobs()
    print('     ├─ [Assert] Checking: len(jobs.pending_jobs(\"tts\")) == 2')
    assert len(jobs.pending_jobs("tts")) == 2
    print('     ├─ [Assert] Checking: len(jobs.pending_jobs(\"ltx\")) == 1')
    assert len(jobs.pending_jobs("ltx")) == 1

    # Start all
    for job_id, _, _ in job_defs:
        jobs.apply(JobStarted(agent="provisioner", job_id=job_id,
                              vm_instance_id="vm-1"))

    print('     ├─ [Assert] Checking: all(j.status == \"running\" for j in jobs.jobs.values())')
    assert all(j.status == "running" for j in jobs.jobs.values())
    print('     ├─ [Assert] Checking: jobs.has_pending_or_running_jobs()')
    assert jobs.has_pending_or_running_jobs()

    # Complete all
    for job_id, _, _ in job_defs:
        jobs.apply(JobCompleted(agent="provisioner", job_id=job_id,
                                artifact_uri=f"/tmp/{job_id}.wav",
                                duration_sec=5.0, vm_instance_id="vm-1"))

    print('     ├─ [Assert] Checking: all(j.status == \"completed\" for j in jobs.jobs.values())')
    assert all(j.status == "completed" for j in jobs.jobs.values())
    print('     ├─ [Assert] Checking: not jobs.has_pending_or_running_jobs()')
    assert not jobs.has_pending_or_running_jobs()

    # Verify artifact URIs
    for job_id, _, _ in job_defs:
        print('     ├─ [Assert] Checking: jobs.jobs[job_id].artifact_uri == f\"/tmp/{job_id}.wav\"')
        assert jobs.jobs[job_id].artifact_uri == f"/tmp/{job_id}.wav"

    print("    ✓ 3-job lifecycle (queue→start→complete) verified")


# ===========================================================================
# 31. Jobs Projection: Dirty/Clean Block Tracking
# ===========================================================================