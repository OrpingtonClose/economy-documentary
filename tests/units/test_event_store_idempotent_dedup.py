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


def test_event_store_idempotent_dedup():

    print('\n▶️  [STARTING TEST] test_event_store_idempotent_dedup')
    """Append the same effect_id twice; verify only one record persists.

    This is the production safety net that prevents duplicate events from
    concurrent writers or network retries.
    Intensity: Light
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        store = EventStore(tmpdir)

        effect = BudgetSet(agent="test", budget_usd=5.0, reason="dedup_test")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        r1 = store.append(effect, otio_hash_before="h1")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        r2 = store.append(effect, otio_hash_before="h2")  # same effect_id

        print('     ├─ [Assert] Checking: r1.seq == r2.seq, (')
        assert r1.seq == r2.seq, (
            f"Dedup failed: first seq={r1.seq}, second seq={r2.seq}"
        )

        all_records = store.read_all()
        print('     ├─ [Assert] Checking: len(all_records) == 1, (')
        assert len(all_records) == 1, (
            f"Expected 1 record after dedup, got {len(all_records)}"
        )

    print("    ✓ idempotent deduplication verified")


# ===========================================================================
# 24. EventStore: Windowed read_since
# ===========================================================================