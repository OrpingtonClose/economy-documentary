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

from capabilities.test_single_purpose_ltx_simulators import LtxSingleSimulator

def test_bdd_multi_scene_video_otio_assembly():

    print('\n▶️  [STARTING TEST] test_bdd_multi_scene_video_otio_assembly')
    """Given 3 completed video jobs with artifacts,
    When the coordinate timeline processes MergeIntoOTIO events,
    Then the timeline has 3 non-overlapping clips in correct order.

    BDD judge evaluates: timeline validity, no overlaps, correct ordering.
    Intensity: Medium (pure computation)
    """
    scenario = BddScenario(
        test_name="multi_scene_video_otio_assembly",
        given="3 completed video jobs with known durations (3.0, 3.5, 4.0 sec)",
        when="MergeIntoOTIO events are applied to CoordinateTimeline",
        then="Timeline has 3 clips on V1_Video track, no overlaps, "
             "total duration ≈ 10.5 seconds",
    )

    ct = CoordinateTimeline()

    # Seed script
    blocks = [
        ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                    text="Block one.", duration_sec=3.0),
        ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator",
                    text="Block two.", duration_sec=3.5),
        ScriptBlock(scene_num=2, block_id="s2_b1", speaker="narrator",
                    text="Block three.", duration_sec=4.0),
    ]
    ct.apply(UpdateScript(agent="scenario", blocks=blocks))

    # Merge 3 clips
    for i, blk in enumerate(blocks):
        ct.apply(MergeIntoOTIO(
            agent="video", job_id=f"j-v-{i+1}", block_id=blk.block_id,
            scene_num=blk.scene_num, slot_id=f"V1:{blk.scene_num}:{blk.block_id}",
            artifact_uri=f"/tmp/clip_{i+1}.mp4", track_name="V1_Video",
            duration_sec=blk.duration_sec,
        ))

    # Mechanical: 3 clips, no overlaps
    video_clips = ct.clips.get("V1_Video", [])
    print('     ├─ [Assert] Checking: len(video_clips) == 3, f\"Expected 3 clips, got {len(video_c...')
    assert len(video_clips) == 3, f"Expected 3 clips, got {len(video_clips)}"

    total_dur = sum(blk.duration_sec for blk in blocks)
    print('     ├─ [Assert] Checking: abs(total_dur - 10.5) < 0.01, f\"Total duration {total_dur} ...')
    assert abs(total_dur - 10.5) < 0.01, f"Total duration {total_dur} != 10.5"

    events = [UpdateScript(agent="scenario", blocks=blocks)] + [
        MergeIntoOTIO(
            agent="video", job_id=f"j-v-{i+1}", block_id=blk.block_id,
            scene_num=blk.scene_num, slot_id=f"V1:{blk.scene_num}:{blk.block_id}",
            artifact_uri=f"/tmp/clip_{i+1}.mp4", track_name="V1_Video",
            duration_sec=blk.duration_sec,
        )
        for i, blk in enumerate(blocks)
    ]
    scenario.evidence = collect_evidence_from_store(
        events,
        projections={
            "clip_count": len(video_clips),
            "total_duration": total_dur,
            "scenario_order": ct.scenario_order,
        },
    )
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bdd_otio_")
    print('     ├─ [BDD Judge] Executing LLM BDD Judge validation...')
    verdict = asyncio.run(run_bdd_judge(scenario, tmp))
    print('     ├─ [Assert] Checking: verdict[\"verdict\"] != \"fail\", f\"BDD judge failed: {verd...')
    assert verdict["verdict"] != "fail", f"BDD judge failed: {verdict['reasoning']}"
    print(f"    ✓ multi-scene OTIO assembly — verdict: {verdict['verdict']}")


# ===========================================================================
# 43. BDD: Audio-Video Duration Alignment — drift handling
# ===========================================================================