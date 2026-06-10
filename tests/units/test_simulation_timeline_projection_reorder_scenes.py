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


def test_simulation_timeline_projection_reorder_scenes():

    print('\n▶️  [STARTING TEST] test_simulation_timeline_projection_reorder_scenes')
    """Build 3-scene timeline, reorder to [3,1,2], verify new clip order.

    Exercises ReorderScenes → track.clear_children + re-append.
    Intensity: Medium
    """
    timeline = Timeline()
    script = UpdateScript(agent="scenario", blocks=[
        ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                    text="Scene one.", duration_sec=3.0),
        ScriptBlock(scene_num=2, block_id="s2_b1", speaker="narrator",
                    text="Scene two.", duration_sec=4.0),
        ScriptBlock(scene_num=3, block_id="s3_b1", speaker="narrator",
                    text="Scene three.", duration_sec=5.0),
    ])
    timeline.apply(script)

    reorder = ReorderScenes(agent="scenario", new_order=[3, 1, 2])
    timeline.apply(reorder)

    # Verify clip order on A1_Narration track
    a1_track = None
    for t in timeline.timeline.tracks:
        if t.name == "A1_Narration":
            a1_track = t
            break
    print('     ├─ [Assert] Checking: a1_track is not None')
    assert a1_track is not None

    clip_names = [c.name for c in a1_track]
    # After reorder: scene 3 first, then 1, then 2
    print('     ├─ [Assert] Checking: \"3\" in clip_names[0], f\"First clip should be scene 3, got...')
    assert "3" in clip_names[0], f"First clip should be scene 3, got {clip_names[0]}"
    print('     ├─ [Assert] Checking: \"1\" in clip_names[1], f\"Second clip should be scene 1, go...')
    assert "1" in clip_names[1], f"Second clip should be scene 1, got {clip_names[1]}"
    print('     ├─ [Assert] Checking: \"2\" in clip_names[2], f\"Third clip should be scene 2, got...')
    assert "2" in clip_names[2], f"Third clip should be scene 2, got {clip_names[2]}"

    print("    ✓ reorder [3,1,2] verified in track clip sequence")


# ===========================================================================
# 29. Timeline Validation: No Overlaps + Track Alignment
# ===========================================================================