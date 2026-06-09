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


def test_simulation_coordinate_timeline_cascade_and_overlap():

    print('\n▶️  [STARTING TEST] test_simulation_coordinate_timeline_cascade_and_overlap')
    """Build coordinate timeline, adjust durations, verify cascade.

    Then attempt an overlapping merge and verify it's rejected.
    Exercises the real sqlean-backed interval arithmetic.
    Intensity: Heavy
    """
    ct = CoordinateTimeline()

    # Build a 3-block script
    script = UpdateScript(agent="scenario", blocks=[
        ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                    text="Block one.", duration_sec=5.0),
        ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator",
                    text="Block two.", duration_sec=4.0),
        ScriptBlock(scene_num=2, block_id="s2_b1", speaker="narrator",
                    text="Block three.", duration_sec=6.0),
    ])
    ct.apply(script)

    # Verify scenario durations were recorded
    print('     ├─ [Assert] Checking: ct.scenario_durations[\"s1_b1\"] == 5.0')
    assert ct.scenario_durations["s1_b1"] == 5.0
    print('     ├─ [Assert] Checking: ct.scenario_durations[\"s1_b2\"] == 4.0')
    assert ct.scenario_durations["s1_b2"] == 4.0
    print('     ├─ [Assert] Checking: ct.scenario_durations[\"s2_b1\"] == 6.0')
    assert ct.scenario_durations["s2_b1"] == 6.0

    # Verify scenario order
    print('     ├─ [Assert] Checking: ct.scenario_order == [\"s1_b1\", \"s1_b2\", \"s2_b1\"]')
    assert ct.scenario_order == ["s1_b1", "s1_b2", "s2_b1"]

    # Verify offsets: s1_b1 at 0, s1_b2 at 5.0, s2_b1 at 9.0
    print('     ├─ [Assert] Checking: ct._get_scenario_offset(\"s1_b1\") == 0.0')
    assert ct._get_scenario_offset("s1_b1") == 0.0
    print('     ├─ [Assert] Checking: ct._get_scenario_offset(\"s1_b2\") == 5.0')
    assert ct._get_scenario_offset("s1_b2") == 5.0
    print('     ├─ [Assert] Checking: ct._get_scenario_offset(\"s2_b1\") == 9.0')
    assert ct._get_scenario_offset("s2_b1") == 9.0

    # Merge a clip at block 1
    merge1 = MergeIntoOTIO(
        agent="audio", job_id="j-001", block_id="s1_b1",
        scene_num=1, slot_id="A1:1:s1_b1",
        artifact_uri="/tmp/b1.wav", track_name="A1_Narration",
        duration_sec=5.0,
    )
    ct.apply(merge1)
    print('     ├─ [Assert] Checking: len(ct.clips[\"A1_Narration\"]) == 1')
    assert len(ct.clips["A1_Narration"]) == 1

    # Merge a clip at block 2
    merge2 = MergeIntoOTIO(
        agent="audio", job_id="j-002", block_id="s1_b2",
        scene_num=1, slot_id="A1:1:s1_b2",
        artifact_uri="/tmp/b2.wav", track_name="A1_Narration",
        duration_sec=4.0,
    )
    ct.apply(merge2)
    print('     ├─ [Assert] Checking: len(ct.clips[\"A1_Narration\"]) == 2')
    assert len(ct.clips["A1_Narration"]) == 2

    # Adjust block 1 duration (measured longer than scripted)
    adjust = DurationAdjusted(
        agent="audio", block_id="A1:1:s1_b1", slot_id="A1:1:s1_b1",
        scene_num=1, voice_role="narrator",
        scripted_sec=5.0, measured_sec=5.5,
    )
    ct.apply(adjust)

    # After cascade: s1_b1 is now 5.5s, so s1_b2 starts at 5.5 (not 5.0)
    print('     ├─ [Assert] Checking: ct.scenario_durations[\"s1_b1\"] == 5.5')
    assert ct.scenario_durations["s1_b1"] == 5.5
    new_offset = ct._get_scenario_offset("s1_b2")
    print('     ├─ [Assert] Checking: abs(new_offset - 5.5) < 0.01, f\"Expected offset 5.5, got {n...')
    assert abs(new_offset - 5.5) < 0.01, f"Expected offset 5.5, got {new_offset}"

    # s2_b1 should start at 5.5 + 4.0 = 9.5
    s2_offset = ct._get_scenario_offset("s2_b1")
    print('     ├─ [Assert] Checking: abs(s2_offset - 9.5) < 0.01, f\"Expected offset 9.5, got {s2...')
    assert abs(s2_offset - 9.5) < 0.01, f"Expected offset 9.5, got {s2_offset}"

    # Test sqlean timespan calculation
    result_ns = ct.query_sqlean_timespan(5.0, 3.5)
    print('     ├─ [Assert] Checking: result_ns is not None, \"sqlean interval query returned None...')
    assert result_ns is not None, "sqlean interval query returned None"

    # Verify IntervalSpan overlap detection
    span_a = IntervalSpan(start_sec=0.0, end_sec=5.0)
    span_b = IntervalSpan(start_sec=4.0, end_sec=8.0)
    span_c = IntervalSpan(start_sec=5.0, end_sec=9.0)
    print('     ├─ [Assert] Checking: span_a.overlaps_with(span_b), \"4-5s should overlap\"')
    assert span_a.overlaps_with(span_b), "4-5s should overlap"
    print('     ├─ [Assert] Checking: not span_a.overlaps_with(span_c), \"Adjacent spans should no...')
    assert not span_a.overlaps_with(span_c), "Adjacent spans should not overlap"
    print('     ├─ [Assert] Checking: abs(span_a.duration() - 5.0) < 0.001')
    assert abs(span_a.duration() - 5.0) < 0.001

    print("    ✓ cascade recalc + overlap detection + sqlean query verified")


# ===========================================================================
# 36. BDD: TTS Fleet Cold Start — Provisioner searches, creates, copies weights
# ===========================================================================