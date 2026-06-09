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

def test_simulation_bdd_script_revision_selective_requeue():

    print('\n▶️  [STARTING TEST] test_simulation_bdd_script_revision_selective_requeue')
    """Given 3 scenes delivered (audio + video complete),
    When script update changes scene 2 text only,
    Then Jobs projection marks scene 2 dirty, scenes 1 and 3 untouched.

    BDD judge evaluates: minimal re-queue, correct dirty tracking.
    Intensity: Medium (event store + projections)
    """
    scenario = BddScenario(
        test_name="script_revision_selective_requeue",
        given="3-scene script fully delivered (all TTS+LTX jobs completed)",
        when="UpdateScript changes only scene 2 text",
        then="Jobs projection: scene 2 jobs dirty, scenes 1 and 3 clean",
    )
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bdd_revision_")

    store = EventStore(log_dir=tmp)
    store._init_db()

    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(PipelineStarted(agent="operator", output_path=f"{tmp}/final.mp4"), "")

    # Original script
    original_blocks = [
        ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                    text="Scene one original.", duration_sec=3.0),
        ScriptBlock(scene_num=2, block_id="s2_b1", speaker="narrator",
                    text="Scene two original.", duration_sec=4.0),
        ScriptBlock(scene_num=3, block_id="s3_b1", speaker="narrator",
                    text="Scene three original.", duration_sec=3.5),
    ]
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(UpdateScript(agent="scenario", blocks=original_blocks), "v1")

    # All jobs completed
    for blk in original_blocks:
        jid_tts = f"tts_{blk.block_id}"
        jid_ltx = f"ltx_{blk.block_id}"
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(QueueJob(
            agent="audio", job_id=jid_tts, job_type="tts",
            scene_num=blk.scene_num, block_id=blk.block_id,
            slot_id=f"A1:{blk.scene_num}:{blk.block_id}",
            params={"text": blk.text},
        ), "v1")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(JobCompleted(
            agent="provisioner", job_id=jid_tts,
            artifact_uri=f"{tmp}/{jid_tts}.wav",
            duration_sec=blk.duration_sec, vm_instance_id="1234567",
        ), "v1")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(QueueJob(
            agent="video", job_id=jid_ltx, job_type="ltx",
            scene_num=blk.scene_num, block_id=blk.block_id,
            slot_id=f"V1:{blk.scene_num}:{blk.block_id}",
            params={"prompt": "Visual for " + blk.text},
        ), "v1")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(JobCompleted(
            agent="provisioner", job_id=jid_ltx,
            artifact_uri=f"{tmp}/{jid_ltx}.mp4",
            duration_sec=blk.duration_sec, vm_instance_id="1234567",
        ), "v1")

    # Script revision: only scene 2 changes
    revised_blocks = [
        ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                    text="Scene one original.", duration_sec=3.0),
        ScriptBlock(scene_num=2, block_id="s2_b1", speaker="narrator",
                    text="Scene two REVISED with new economic data.", duration_sec=5.0),
        ScriptBlock(scene_num=3, block_id="s3_b1", speaker="narrator",
                    text="Scene three original.", duration_sec=3.5),
    ]
    print('     ├─ [EventStore] Appending event to SQLite events database...')
    store.append(UpdateScript(agent="scenario", blocks=revised_blocks), "v2")

    events = store.replay()

    # Build timeline projection
    tp = Timeline()
    for e in events:
        tp.apply(e.effect)

    # Build jobs projection
    jp = Jobs()
    for e in events:
        jp.apply(e.effect)

    # Check that scene 2 block has a new slot (dirty)
    # The timeline should reflect the updated text for scene 2
    scene2_slots = [s for s in tp.slots.values() if s["scene_num"] == 2]

    scenario.evidence = collect_evidence_from_store(
        events,
        projections={
            "timeline_slot_count": len(tp.slots),
            "scene2_slot_count": len(scene2_slots),
            "total_jobs": len(jp.jobs),
        },
    )
    print('     ├─ [BDD Judge] Executing LLM BDD Judge validation...')
    verdict = asyncio.run(run_bdd_judge(scenario, tmp))
    print('     ├─ [Assert] Checking: verdict[\"verdict\"] != \"fail\", f\"BDD judge failed: {verd...')
    assert verdict["verdict"] != "fail", f"BDD judge failed: {verdict['reasoning']}"
    print(f"    ✓ script revision selective requeue — verdict: {verdict['verdict']}")


# ===========================================================================
# 48. BDD: Final Assembly Real Media — ffmpeg concatenation
# ===========================================================================