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


def test_event_store_read_since_window():

    print('\n▶️  [STARTING TEST] test_event_store_read_since_window')
    """Append 10 events, read_since(seq=5), verify only later events returned.

    Exercises the incremental read path that projections use to catch up.
    Intensity: Medium
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        store = EventStore(tmpdir)

        seqs = []
        for i in range(10):
            effect = BudgetSet(agent="test", budget_usd=float(i + 1),
                               reason=f"window_{i}")
            print('     ├─ [EventStore] Appending event to SQLite events database...')
            rec = store.append(effect, otio_hash_before="h")
            seqs.append(rec.seq)

        # Read since the 5th event
        mid_seq = seqs[4]
        window = store.read_since(mid_seq)
        expected_count = len([s for s in seqs if s > mid_seq])
        print('     ├─ [Assert] Checking: len(window) == expected_count, (')
        assert len(window) == expected_count, (
            f"Expected {expected_count} events after seq {mid_seq}, got {len(window)}"
        )

        # Verify all returned seqs are > mid_seq
        for rec in window:
            print('     ├─ [Assert] Checking: rec.seq > mid_seq')
            assert rec.seq > mid_seq

    print("    ✓ windowed read_since verified with 10-event stream")


# ===========================================================================
# 25. Timeline Projection: Script → Slot Creation
# ===========================================================================