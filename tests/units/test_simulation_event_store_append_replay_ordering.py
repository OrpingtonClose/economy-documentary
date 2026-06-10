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


def test_simulation_event_store_append_replay_ordering():

    print('\n▶️  [STARTING TEST] test_simulation_event_store_append_replay_ordering')
    """Append 10 effects of mixed kinds, replay, verify monotonic sequences.

    Exercises the real SQLite WAL path with schema creation, append, and
    full replay. Verifies seq monotonicity and kind preservation.
    Intensity: Medium
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        store = EventStore(tmpdir)

        kinds_emitted = []
        for i in range(10):
            if i % 3 == 0:
                effect = BudgetSet(agent="test", budget_usd=10.0 + i,
                                   reason="test_round")
            elif i % 3 == 1:
                effect = QueueJob(agent="test", job_id=f"j-{i:03d}",
                                  job_type="tts", scene_num=1,
                                  block_id=f"b{i}", slot_id=f"A1:1:b{i}")
            else:
                effect = NoOp(agent="test", reason=f"filler_{i}")

            kinds_emitted.append(effect.kind)
            print('     ├─ [EventStore] Appending event to SQLite events database...')
            store.append(effect, otio_hash_before="hash-0")

        records = store.replay()
        # NoOps are not persisted (seq=-1), so filter them out
        non_noop = [k for k in kinds_emitted if k != "noop"]
        print('     ├─ [Assert] Checking: len(records) == len(non_noop), (')
        assert len(records) == len(non_noop), (
            f"Expected {len(non_noop)} persisted, got {len(records)}"
        )

        # Verify monotonic sequence
        for i in range(1, len(records)):
            print('     ├─ [Assert] Checking: records[i].seq > records[i - 1].seq, \"Sequence not monotoni...')
            assert records[i].seq > records[i - 1].seq, "Sequence not monotonic"

        # Verify kind preservation
        replay_kinds = [r.effect.kind for r in records]
        print('     ├─ [Assert] Checking: replay_kinds == non_noop')
        assert replay_kinds == non_noop

    print("    ✓ 10-event append/replay with monotonic ordering verified")


# ===========================================================================
# 23. EventStore: Idempotent Deduplication
# ===========================================================================