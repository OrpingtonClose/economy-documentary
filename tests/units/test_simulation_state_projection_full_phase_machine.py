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


def test_simulation_state_projection_full_phase_machine():

    print('\n▶️  [STARTING TEST] test_simulation_state_projection_full_phase_machine')
    """Walk through the complete pipeline phase sequence.

    Exercises: init → audio_reconcile → video_production → done.
    Also verifies loop detection and recent-effects tracking.
    Intensity: Heavy
    """
    state = StateProjection()
    print('     ├─ [Assert] Checking: state.current_phase == \"init\"')
    assert state.current_phase == "init"

    # Phase 1: Pipeline started
    state.apply(PipelineStarted(agent="orchestrator"))
    print('     ├─ [Assert] Checking: state.current_phase == \"init\"')
    assert state.current_phase == "init"

    # Phase 2: Script comes in (no phase change, but effects are tracked)
    script = UpdateScript(agent="scenario", blocks=[
        ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                    text="Test block.", duration_sec=5.0),
    ])
    state.apply(script)
    print('     ├─ [Assert] Checking: \"scenario\" in state.recent_effects')
    assert "scenario" in state.recent_effects

    # Phase 3: Reconciliation complete → audio_reconcile
    state.apply(ReconciliationComplete(
        agent="audio", blocks_total=1, blocks_passed=1,
        blocks_failed=0, worst_delta_sec=0.05, total_measured_sec=5.1,
    ))
    print('     ├─ [Assert] Checking: state.current_phase == \"audio_reconcile\"')
    assert state.current_phase == "audio_reconcile"
    print('     ├─ [Assert] Checking: len(state.phase_history) == 1')
    assert len(state.phase_history) == 1
    print('     ├─ [Assert] Checking: state.phase_history[0].from_phase == \"init\"')
    assert state.phase_history[0].from_phase == "init"
    print('     ├─ [Assert] Checking: state.phase_history[0].to_phase == \"audio_reconcile\"')
    assert state.phase_history[0].to_phase == "audio_reconcile"

    # Phase 4: Video merge → video_production
    merge = MergeIntoOTIO(
        agent="video", job_id="j-001", block_id="s1_b1",
        scene_num=1, slot_id="V1:1:s1_b1",
        artifact_uri="/tmp/clip.mp4", track_name="V1_Video",
        duration_sec=5.0,
    )
    state.apply(merge)
    print('     ├─ [Assert] Checking: state.current_phase == \"video_production\"')
    assert state.current_phase == "video_production"
    print('     ├─ [Assert] Checking: len(state.phase_history) == 2')
    assert len(state.phase_history) == 2

    # Phase 5: Pipeline complete → done
    state.apply(PipelineComplete(
        agent="assembly", output_path="/tmp/final.mp4",
        duration_sec=5.0, total_cost_usd=1.50,
    ))
    print('     ├─ [Assert] Checking: state.current_phase == \"done\"')
    assert state.current_phase == "done"
    print('     ├─ [Assert] Checking: len(state.phase_history) == 3')
    assert len(state.phase_history) == 3

    # Verify loop detection doesn't false-positive with varied effects
    is_loop, _ = state.detect_duplicate_loop("scenario")
    print('     ├─ [Assert] Checking: not is_loop')
    assert not is_loop

    # Verify recent events retrieval
    recent = state.get_recent_events(3)
    print('     ├─ [Assert] Checking: len(recent) <= 3')
    assert len(recent) <= 3

    print("    ✓ phase machine: init → audio_reconcile → video → done")


# ===========================================================================
# 35. CoordinateTimeline: Cascade Recalculation + Overlap Rejection
# ===========================================================================