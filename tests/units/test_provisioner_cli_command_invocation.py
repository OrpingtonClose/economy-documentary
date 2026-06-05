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


def test_provisioner_cli_command_invocation():

    print('\n▶️  [STARTING TEST] test_provisioner_cli_command_invocation')
    """Verify that the Provisioner executes the correct CLI commands without simulator capabilities."""
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "provisioner"]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        provisioner_port = harness.ports["provisioner"]
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        # Queue a pending job
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(QueueJob(
            agent="audio", job_id="job_tts_1", job_type="tts",
            scene_num=1, block_id="s1_b1", slot_id="s1_b1",
            params={"text": "Hello", "voice": "narrator", "gpu_type": "RTX 4090"}
        ), "")
        
        # Wake up Provisioner (this will trigger search, create, and copy command executions)
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{provisioner_port}/", content="Wakeup", timeout=None)
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200
        
        # Trigger VM deallocation to check destroy command execution
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(VMObserved(
            agent="provisioner",
            instance_id="1234567",
            observed_status="not_found",
            expected_status="running",
            drift_description="drift",
            corrective_action="none"
        ), "")
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{provisioner_port}/", content="Wakeup", timeout=None)
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200
        
        # Verify that mock vastai CLI script was executed and recorded the commands
        log_path = os.path.join(db_dir, "vastai_invocations.log")
        print('     ├─ [Assert] Checking: os.path.exists(log_path), \"Mock vastai log was not generate...')
        assert os.path.exists(log_path), "Mock vastai log was not generated"
        
        with open(log_path) as f:
            invocations = f.read().splitlines()
            
        print(f"Mock Vast.ai Invocations: {invocations}")
        print('     ├─ [Assert] Checking: any(\"search offers\" in cmd for cmd in invocations)')
        assert any("search offers" in cmd for cmd in invocations)
        print('     ├─ [Assert] Checking: any(\"create instance\" in cmd for cmd in invocations)')
        assert any("create instance" in cmd for cmd in invocations)
        print('     ├─ [Assert] Checking: any(\"copy\" in cmd for cmd in invocations)')
        assert any("copy" in cmd for cmd in invocations)
        print('     ├─ [Assert] Checking: any(\"show instances\" in cmd or \"show instance\" in cmd fo...')
        assert any("show instances" in cmd or "show instance" in cmd for cmd in invocations)
        print('     ├─ [Assert] Checking: any(\"destroy instance\" in cmd for cmd in invocations)')
        assert any("destroy instance" in cmd for cmd in invocations)


# ===========================================================================
# 18. Real Qwen3-TTS Script Execution (GPU-mocked, logic real)
# ===========================================================================