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


def test_coordinate_timeline_dynamic_drift():

    print('\n▶️  [STARTING TEST] test_coordinate_timeline_dynamic_drift')
    """Verify that DurationAdjusted events update start/end coordinates of slots."""
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa"]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        # Initialize script with 3 blocks
        blocks = [
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="First text.", duration_sec=3.0),
            ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator", text="Second text.", duration_sec=3.0),
            ScriptBlock(scene_num=1, block_id="s1_b3", speaker="narrator", text="Third text.", duration_sec=3.0)
        ]
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")
        
        # Adjust duration of block 1 (increase by 2.0s)
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(DurationAdjusted(
            agent="audio", block_id="A1:1:s1_b1", slot_id="A1:1:s1_b1",
            scene_num=1, voice_role="narrator", scripted_sec=3.0, measured_sec=5.0
        ), "initial_hash")
        
        # Poll GSA and verify dynamic cascade of timings
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.get(f"http://127.0.0.1:{gsa_port}/")
        state = resp.json()
        print('     ├─ [Assert] Checking: state[\"otio\"][\"duration_sec\"] == 11.0 # 5.0 + 3.0 + 3.0 ...')
        assert state["otio"]["duration_sec"] == 11.0 # 5.0 + 3.0 + 3.0 = 11.0s


    # ===========================================================================
    # 10. Budget Limit Aborted Gate
    # ===========================================================================
