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


def test_simulation_timeline_projection_delete_scene():

    print('\n▶️  [STARTING TEST] test_simulation_timeline_projection_delete_scene')
    """Apply script for 2 scenes, delete scene 1, verify only scene 2 survives.

    Exercises DeleteScene → clip removal + slot eviction.
    Intensity: Medium
    """
    timeline = Timeline()
    script = UpdateScript(agent="scenario", blocks=[
        ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                    text="This scene will be deleted.", duration_sec=4.0),
        ScriptBlock(scene_num=2, block_id="s2_b1", speaker="narrator",
                    text="This scene survives.", duration_sec=5.0),
    ])
    timeline.apply(script)
    print('     ├─ [Assert] Checking: len(timeline.slots) == 4  # 2 blocks × 2 tracks')
    assert len(timeline.slots) == 4  # 2 blocks × 2 tracks

    # Delete scene 1
    delete = DeleteScene(agent="scenario", scene_num=1, reason="off-topic")
    timeline.apply(delete)

    # Only scene 2 slots should remain
    print('     ├─ [Assert] Checking: len(timeline.slots) == 2, f\"Expected 2 slots, got {len(time...')
    assert len(timeline.slots) == 2, f"Expected 2 slots, got {len(timeline.slots)}"
    for key in timeline.slots:
        print('     ├─ [Assert] Checking: \":2:\" in key, f\"Unexpected slot {key} survived deletion\"')
        assert ":2:" in key, f"Unexpected slot {key} survived deletion"

    print("    ✓ delete scene 1 removed 2 slots, scene 2 intact")


# ===========================================================================
# 28. Timeline Projection: Reorder Scenes
# ===========================================================================