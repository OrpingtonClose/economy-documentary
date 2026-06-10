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

from capabilities.test_single_purpose_tts_simulators import TtsJob1Simulator

def test_simulation_bdd_audio_video_duration_alignment():

    print('\n▶️  [STARTING TEST] test_simulation_bdd_audio_video_duration_alignment')
    """Given TTS produced audio with measured duration different from scripted,
    When DurationAdjusted is applied,
    Then downstream block offsets cascade correctly.

    BDD judge evaluates: offset correctness, drift < 0.5s tolerance.
    Intensity: Medium (pure computation)
    """
    scenario = BddScenario(
        test_name="audio_video_duration_alignment",
        given="Script: 2 blocks (3.0s, 4.0s). TTS measured block 1 at 3.7s (0.7s drift)",
        when="DurationAdjusted event applied to CoordinateTimeline",
        then="Block 2 offset shifts from 3.0 to 3.7, total timeline adjusts accordingly",
    )

    ct = CoordinateTimeline()
    blocks = [
        ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                    text="First block.", duration_sec=3.0),
        ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator",
                    text="Second block.", duration_sec=4.0),
    ]
    ct.apply(UpdateScript(agent="scenario", blocks=blocks))

    # Merge audio clips
    ct.apply(MergeIntoOTIO(
        agent="audio", job_id="j-a-1", block_id="s1_b1",
        scene_num=1, slot_id="A1:1:s1_b1",
        artifact_uri="/tmp/b1.wav", track_name="A1_Narration", duration_sec=3.0,
    ))

    # Duration drift
    ct.apply(DurationAdjusted(
        agent="audio", block_id="s1_b1", slot_id="A1:1:s1_b1",
        scene_num=1, voice_role="narrator",
        scripted_sec=3.0, measured_sec=3.7,
    ))

    # Mechanical: block 2 offset should now be 3.7
    offset_b2 = ct._get_scenario_offset("s1_b2")
    print('     ├─ [Assert] Checking: abs(offset_b2 - 3.7) < 0.01, f\"Expected offset 3.7, got {of...')
    assert abs(offset_b2 - 3.7) < 0.01, f"Expected offset 3.7, got {offset_b2}"
    print('     ├─ [Assert] Checking: ct.scenario_durations[\"s1_b1\"] == 3.7')
    assert ct.scenario_durations["s1_b1"] == 3.7

    events = [
        UpdateScript(agent="scenario", blocks=blocks),
        MergeIntoOTIO(
            agent="audio", job_id="j-a-1", block_id="s1_b1",
            scene_num=1, slot_id="A1:1:s1_b1",
            artifact_uri="/tmp/b1.wav", track_name="A1_Narration", duration_sec=3.0,
        ),
        DurationAdjusted(
            agent="audio", block_id="s1_b1", slot_id="A1:1:s1_b1",
            scene_num=1, voice_role="narrator",
            scripted_sec=3.0, measured_sec=3.7,
        )
    ]
    scenario.evidence = collect_evidence_from_store(
        events,
        projections={
            "block1_duration": ct.scenario_durations["s1_b1"],
            "block2_offset": offset_b2,
            "drift_sec": 0.7,
        },
    )
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bdd_drift_")
    print('     ├─ [BDD Judge] Executing LLM BDD Judge validation...')
    verdict = asyncio.run(run_bdd_judge(scenario, tmp))
    print('     ├─ [Assert] Checking: verdict[\"verdict\"] != \"fail\", f\"BDD judge failed: {verd...')
    assert verdict["verdict"] != "fail", f"BDD judge failed: {verdict['reasoning']}"
    print(f"    ✓ audio-video duration alignment — verdict: {verdict['verdict']}")


# ===========================================================================
# 44. BDD: TTS Retry After Failure — error recovery path
# ===========================================================================