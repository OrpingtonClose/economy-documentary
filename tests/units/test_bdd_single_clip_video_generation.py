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

def test_bdd_single_clip_video_generation():

    print('\n▶️  [STARTING TEST] test_bdd_single_clip_video_generation')
    """Given an LTX VM is active and 1 LTX job is pending,
    When the video agent wakes up,
    Then it dispatches the job and produces a video artifact.

    BDD judge evaluates: artifact existence, event emission, plausible duration.
    Intensity: Heavy (real harness, 3 agents)
    """
    scenario = BddScenario(
        test_name="single_clip_video_generation",
        given="LTX VM allocated (instance 1234567), 1 pending LTX job",
        when="Video agent dispatches LTX job to the active VM",
        then="A video artifact is produced (or referenced), "
             "JobStarted and JobCompleted events emitted",
    )
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "video", "provisioner"]) as harness:
        db_dir = harness.temp_dir.name
        video_port = harness.ports["video"]

        store = EventStore(log_dir=db_dir)
        store._init_db()

        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(BudgetSet(agent="operator", budget_usd=5.0), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(UpdateScript(agent="scenario", blocks=[
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                        text="Central banks shape monetary policy.", duration_sec=3.5),
        ]), "init")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(VMAllocated(
            agent="provisioner", instance_id="1234567", role="ltx",
            offer_id="101", worker_url="http://127.0.0.1:9001",
            gpu_type="RTX 4090", cost_per_hour=0.85,
        ), "init")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(QueueJob(
            agent="video", job_id="job_ltx_single_1", job_type="ltx",
            scene_num=1, block_id="s1_b1", slot_id="V1:1:s1_b1",
            params={"prompt": "Aerial view of central bank building.", "gpu_type": "RTX 4090"},
        ), "init")

        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{video_port}/", content="Wakeup", timeout=None)
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200

        events = store.replay()
        completed = [e for e in events if e.effect.kind == "JobCompleted"]

        scenario.evidence = collect_evidence_from_store(
            events,
            projections={"completed_jobs": len(completed)},
        )
        print('     ├─ [BDD Judge] Executing LLM BDD Judge validation...')
        verdict = asyncio.run(run_bdd_judge(scenario, db_dir))
        print('     ├─ [Assert] Checking: verdict[\"verdict\"] != \"fail\", f\"BDD judge failed: {verd...')
        assert verdict["verdict"] != "fail", f"BDD judge failed: {verdict['reasoning']}"
        print(f"    ✓ single clip video generation — verdict: {verdict['verdict']}")


# ===========================================================================
# 42. BDD: Multi-Scene Video OTIO Assembly — batch video + timeline
# ===========================================================================