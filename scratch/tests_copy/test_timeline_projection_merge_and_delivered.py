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


def test_timeline_projection_merge_and_delivered():

    print('\n▶️  [STARTING TEST] test_timeline_projection_merge_and_delivered')
    """Apply script + merge, verify slot transitions to 'delivered'.

    Exercises the MergeIntoOTIO → ExternalReference attachment path
    and status flip from 'scripted' → 'delivered'.
    Intensity: Medium
    """
    import opentimelineio as otio

    timeline = Timeline()

    # Step 1: Build slots from script
    script = UpdateScript(agent="scenario", blocks=[
        ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                    text="Neural pathways form through repetition.",
                    duration_sec=5.0),
    ])
    timeline.apply(script)

    # Step 2: Merge an approved audio clip into A1
    merge = MergeIntoOTIO(
        agent="audio", job_id="j-audio-001", block_id="s1_b1",
        scene_num=1, slot_id="A1:1:s1_b1",
        artifact_uri="s3://bucket/scene1_b1.wav",
        track_name="A1_Narration", duration_sec=5.2,
    )
    timeline.apply(merge)

    # Verify status flipped
    slot = timeline.slots["A1:1:s1_b1"]
    print('     ├─ [Assert] Checking: slot[\"status\"] == \"delivered\", f\"Expected \'delivered\'...')
    assert slot["status"] == "delivered", f"Expected 'delivered', got {slot['status']}"
    print('     ├─ [Assert] Checking: slot[\"artifact_uri\"] == \"s3://bucket/scene1_b1.wav\"')
    assert slot["artifact_uri"] == "s3://bucket/scene1_b1.wav"
    print('     ├─ [Assert] Checking: slot[\"measured_sec\"] == 5.2')
    assert slot["measured_sec"] == 5.2

    # Verify the OTIO clip now has an ExternalReference (not MissingReference)
    clip = timeline._find_clip_by_name("A1:1:s1_b1")
    print('     ├─ [Assert] Checking: clip is not None')
    assert clip is not None
    print('     ├─ [Assert] Checking: isinstance(clip.media_reference, otio.schema.ExternalReferen...')
    assert isinstance(clip.media_reference, otio.schema.ExternalReference)
    print('     ├─ [Assert] Checking: clip.media_reference.target_url == \"s3://bucket/scene1_b1.w...')
    assert clip.media_reference.target_url == "s3://bucket/scene1_b1.wav"

    print("    ✓ merge → delivered status + ExternalReference verified")


# ===========================================================================
# 27. Timeline Projection: Delete Scene Removes All Slots
# ===========================================================================