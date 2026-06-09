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


def test_simulation_timeline_projection_script_to_slots():

    print('\n▶️  [STARTING TEST] test_simulation_timeline_projection_script_to_slots')
    """Apply UpdateScript to Timeline projection, verify slot structure.

    Exercises the core OTIO-building path: script blocks → tracks → clips.
    Verifies slot count (2x blocks for A1+V1), status, and field accuracy.
    Intensity: Medium
    """
    timeline = Timeline()
    script = UpdateScript(agent="scenario", blocks=[
        ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                    text="The economy of attention is collapsing.",
                    duration_sec=4.5, visual_notes="Wide shot of city"),
        ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator",
                    text="Dopamine loops keep users trapped.",
                    duration_sec=3.2, visual_notes="Close-up of phone screen"),
        ScriptBlock(scene_num=2, block_id="s2_b1", speaker="guest_a",
                    text="Regulation has failed to keep pace.",
                    duration_sec=6.0, visual_notes="Parliament exterior"),
    ])
    timeline.apply(script)

    # 3 blocks × 2 tracks (A1 + V1) = 6 slots
    print('     ├─ [Assert] Checking: len(timeline.slots) == 6, f\"Expected 6 slots, got {len(time...')
    assert len(timeline.slots) == 6, f"Expected 6 slots, got {len(timeline.slots)}"

    # Verify slot keys exist for both tracks
    for prefix in ("A1", "V1"):
        for block_id in ("s1_b1", "s1_b2", "s2_b1"):
            scene_num = 1 if block_id.startswith("s1") else 2
            key = f"{prefix}:{scene_num}:{block_id}"
            print('     ├─ [Assert] Checking: key in timeline.slots, f\"Missing slot {key}\"')
            assert key in timeline.slots, f"Missing slot {key}"
            slot = timeline.slots[key]
            print('     ├─ [Assert] Checking: slot[\"status\"] == \"scripted\"')
            assert slot["status"] == "scripted"
            print('     ├─ [Assert] Checking: slot[\"artifact_uri\"] is None')
            assert slot["artifact_uri"] is None
            print('     ├─ [Assert] Checking: slot[\"measured_sec\"] is None')
            assert slot["measured_sec"] is None

    # Verify tracks were created
    track_names = [t.name for t in timeline.timeline.tracks]
    print('     ├─ [Assert] Checking: \"A1_Narration\" in track_names')
    assert "A1_Narration" in track_names
    print('     ├─ [Assert] Checking: \"V1_Video\" in track_names')
    assert "V1_Video" in track_names

    # Verify timeline duration is sum of all blocks
    dur = timeline.get_timeline_duration_sec()
    expected_dur = 4.5 + 3.2 + 6.0  # 13.7
    print('     ├─ [Assert] Checking: abs(dur - expected_dur) < 0.1, f\"Expected ~{expected_dur}s,...')
    assert abs(dur - expected_dur) < 0.1, f"Expected ~{expected_dur}s, got {dur}s"

    print("    ✓ 3-block script produced 6 OTIO slots across A1/V1")


# ===========================================================================
# 26. Timeline Projection: Merge Clip → Delivered Status
# ===========================================================================