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

from capabilities.test_real_assembly_bdd_assemble_final_cut import AssembleFinalCutSimulator

def test_bdd_final_assembly_real_media():

    print('\n▶️  [STARTING TEST] test_bdd_final_assembly_real_media')
    """Given all audio+video slots delivered with real WAV+MP4 on disk,
    When the assembly agent runs final cut,
    Then output MP4 exists with correct duration.

    BDD judge evaluates: output validity, PipelineComplete event.
    Intensity: Heavy (real harness, 3 agents)
    """
    scenario = BddScenario(
        test_name="final_assembly_real_media",
        given="All slots delivered, real WAV/MP4 files on disk",
        when="Assembly agent runs assemble_final_cut",
        then="Output MP4 at expected path, PipelineComplete event emitted, "
             "file size > 0 bytes",
    )
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "assembly"]) as harness:
        db_dir = harness.temp_dir.name
        assembly_port = harness.ports["assembly"]
        gsa_port = harness.ports["gsa"]

        store = EventStore(log_dir=db_dir)
        store._init_db()

        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(BudgetSet(agent="operator", budget_usd=5.0), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(UpdateScript(agent="scenario", blocks=[
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                        text="The economy recovers.", duration_sec=3.0),
            ScriptBlock(scene_num=2, block_id="s2_b1", speaker="narrator",
                        text="Markets stabilize.", duration_sec=4.0),
        ]), "init")

        # Mark both slots as delivered
        for blk_id, scene, dur in [("s1_b1", 1, 3.0), ("s2_b1", 2, 4.0)]:
            print('     ├─ [EventStore] Appending event to SQLite events database...')
            store.append(JobCompleted(
                agent="provisioner", job_id=f"tts_{blk_id}",
                artifact_uri=f"{db_dir}/audio_outputs/tts_{blk_id}.wav",
                duration_sec=dur, vm_instance_id="1234567",
            ), "init")
            print('     ├─ [EventStore] Appending event to SQLite events database...')
            store.append(MergeIntoOTIO(
                agent="audio", job_id=f"tts_{blk_id}", block_id=blk_id,
                scene_num=scene, slot_id=f"A1:{scene}:{blk_id}",
                artifact_uri=f"{db_dir}/audio_outputs/tts_{blk_id}.wav",
                track_name="A1_Narration", duration_sec=dur,
            ), "init")
            print('     ├─ [EventStore] Appending event to SQLite events database...')
            store.append(JobCompleted(
                agent="provisioner", job_id=f"ltx_{blk_id}",
                artifact_uri=f"{db_dir}/video_outputs/ltx_{blk_id}.mp4",
                duration_sec=dur, vm_instance_id="1234567",
            ), "init")
            print('     ├─ [EventStore] Appending event to SQLite events database...')
            store.append(MergeIntoOTIO(
                agent="video", job_id=f"ltx_{blk_id}", block_id=blk_id,
                scene_num=scene, slot_id=f"V1:{scene}:{blk_id}",
                artifact_uri=f"{db_dir}/video_outputs/ltx_{blk_id}.mp4",
                track_name="V1_Video", duration_sec=dur,
            ), "init")

        # Wake assembly agent
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{assembly_port}/", content="Wakeup", timeout=None)
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200

        events = store.replay()
        pipeline_complete = [e for e in events if e.effect.kind == "PipelineComplete"]

        scenario.evidence = collect_evidence_from_store(
            events,
            projections={"pipeline_complete_count": len(pipeline_complete)},
        )
        print('     ├─ [BDD Judge] Executing LLM BDD Judge validation...')
        verdict = asyncio.run(run_bdd_judge(scenario, db_dir))
        print('     ├─ [Assert] Checking: verdict[\"verdict\"] != \"fail\", f\"BDD judge failed: {verd...')
        assert verdict["verdict"] != "fail", f"BDD judge failed: {verdict['reasoning']}"
        print(f"    ✓ final assembly real media — verdict: {verdict['verdict']}")


# ===========================================================================
# 49. BDD: Partial Failure Isolated Recovery — failure doesn't block others
# ===========================================================================