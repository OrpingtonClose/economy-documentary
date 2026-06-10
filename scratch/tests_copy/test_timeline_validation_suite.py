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


def test_timeline_validation_suite():

    print('\n▶️  [STARTING TEST] test_timeline_validation_suite')
    """Build a valid timeline, run all three validators, assert pass.

    Exercises validate_no_overlaps, validate_track_alignment, and
    validate_clip_media (before merge = missing refs).
    Intensity: Medium
    """
    timeline = Timeline()
    script = UpdateScript(agent="scenario", blocks=[
        ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                    text="First block.", duration_sec=5.0),
        ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator",
                    text="Second block.", duration_sec=3.0),
    ])
    timeline.apply(script)

    # No overlaps in a freshly built timeline
    ok, msg = timeline.validate_no_overlaps()
    print('     ├─ [Assert] Checking: ok, f\"Unexpected overlap: {msg}\"')
    assert ok, f"Unexpected overlap: {msg}"

    # Track alignment should pass (both tracks have same total duration)
    ok, msg = timeline.validate_track_alignment()
    print('     ├─ [Assert] Checking: ok, f\"Track misalignment: {msg}\"')
    assert ok, f"Track misalignment: {msg}"

    # Clip media should fail (all MissingReference before merge)
    ok, msg = timeline.validate_clip_media()
    print('     ├─ [Assert] Checking: not ok, \"Expected clip media validation to fail before merg...')
    assert not ok, "Expected clip media validation to fail before merges"
    print('     ├─ [Assert] Checking: \"no media reference\" in (msg or \"\").lower()')
    assert "no media reference" in (msg or "").lower()

    # Now merge one clip and verify partial state
    merge = MergeIntoOTIO(
        agent="audio", job_id="j-001", block_id="s1_b1",
        scene_num=1, slot_id="A1:1:s1_b1",
        artifact_uri="/tmp/test.wav", track_name="A1_Narration",
        duration_sec=5.0,
    )
    timeline.apply(merge)

    # Still not all filled
    print('     ├─ [Assert] Checking: not timeline.all_slots_filled()')
    assert not timeline.all_slots_filled()

    print("    ✓ overlap/alignment/media validation suite passed")


# ===========================================================================
# 30. Jobs Projection: Full Lifecycle (queue → start → complete)
# ===========================================================================