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

def test_scenario_agent_live_prompt_turn():

    print('\n▶️  [STARTING TEST] test_scenario_agent_live_prompt_turn')
    deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    if not os.path.exists(deepseek_key_path):
        pytest.skip("DeepSeek API key is missing. Skipping live Scenario Agent prompt turn test.")

    # Check network reachability for deepseek API
    import socket
    try:
        socket.setdefaulttimeout(2.0)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("api.deepseek.com", 443))
    except Exception:
        pytest.skip("api.deepseek.com is unreachable (offline/restricted network). Skipping live Scenario Agent prompt turn test.")

    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "scenario"]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        scenario_port = harness.ports["scenario"]
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        
        # Prompt Scenario Agent to partition a short text into blocks
        prompt = "Create a script with 2 blocks about global interest rates."
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{scenario_port}/", content=prompt, timeout=None)
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200
        
        # Verify script block creation in GSA
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
        print('     ├─ [Assert] Checking: len(gsa_resp[\"otio\"][\"slots\"]) >= 1')
        assert len(gsa_resp["otio"]["slots"]) >= 1


    # ===========================================================================
    # 3. Audio Agent TTS Job Queueing
    # ===========================================================================
