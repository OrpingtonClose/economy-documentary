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


def test_simulation_parse_duration_all_formats():

    print('\n▶️  [STARTING TEST] test_simulation_parse_duration_all_formats')
    """Verify parse_duration handles every supported input shape.

    Covers: int, float, numeric string, "MM:SS", "HH:MM:SS", whitespace.
    Intensity: Light
    """
    # Integer
    print('     ├─ [Assert] Checking: parse_duration(15) == 15.0')
    assert parse_duration(15) == 15.0
    # Float
    print('     ├─ [Assert] Checking: parse_duration(7.25) == 7.25')
    assert parse_duration(7.25) == 7.25
    # Numeric string
    print('     ├─ [Assert] Checking: parse_duration(\"42.5\") == 42.5')
    assert parse_duration("42.5") == 42.5
    # MM:SS
    print('     ├─ [Assert] Checking: parse_duration(\"2:30\") == 150.0')
    assert parse_duration("2:30") == 150.0
    # HH:MM:SS
    print('     ├─ [Assert] Checking: parse_duration(\"1:02:30\") == 3750.0')
    assert parse_duration("1:02:30") == 3750.0
    # Whitespace-padded
    print('     ├─ [Assert] Checking: parse_duration(\"  3:15  \") == 195.0')
    assert parse_duration("  3:15  ") == 195.0
    # Zero
    print('     ├─ [Assert] Checking: parse_duration(0) == 0.0')
    assert parse_duration(0) == 0.0
    print('     ├─ [Assert] Checking: parse_duration(\"0\") == 0.0')
    assert parse_duration("0") == 0.0
    # Fractional seconds in MM:SS
    print('     ├─ [Assert] Checking: abs(parse_duration(\"1:30.5\") - 90.5) < 0.001')
    assert abs(parse_duration("1:30.5") - 90.5) < 0.001
    # Verify error on garbage
    raised = False
    try:
        parse_duration("not_a_duration")
    except ValueError:
        raised = True
    print('     ├─ [Assert] Checking: raised, \"parse_duration should raise ValueError on garbage ...')
    assert raised, "parse_duration should raise ValueError on garbage input"
    print("    ✓ all duration format variants validated")


# ===========================================================================
# 21. Effect Pydantic Round-Trip Serialization
# ===========================================================================