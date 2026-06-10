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

from capabilities.test_single_purpose_tts_simulators import TtsPreemptSimulator
from capabilities.test_real_vast_provisioning_bdd_search_offers import VastSearchSimulator
from capabilities.test_real_vast_provisioning_bdd_create_instance import VastCreateSimulator

def test_preemption_and_recovery():

    print('\n▶️  [STARTING TEST] test_preemption_and_recovery')
    """Verify that the Provisioner Agent detects VM preemption and recovers cleanly."""
    from effects import VMObserved
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "provisioner"]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        provisioner_port = harness.ports["provisioner"]
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        
        # Seed active VM and running job
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(VMAllocated(
            agent="provisioner", instance_id="vm_preempted", role="tts",
            offer_id="101", worker_url="http://127.0.0.1:9001",
            gpu_type="RTX 4090", cost_per_hour=0.50
        ), "")
        
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(QueueJob(
            agent="audio", job_id="job_tts_1", job_type="tts",
            scene_num=1, block_id="s1_b1", slot_id="A1:1:s1_b1",
            params={"text": "Hello", "voice": "narrator", "gpu_type": "RTX 4090"}
        ), "")
        
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(JobStarted(agent="provisioner", job_id="job_tts_1", vm_instance_id="vm_preempted"), "")
        
        # Seed preemption drift observation
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(VMObserved(
            agent="provisioner", instance_id="vm_preempted",
            observed_status="not_found", expected_status="running",
            drift_description="Vast.ai reports instance destroyed",
            corrective_action="escalate"
        ), "")
        
        # Wake up Provisioner Agent to handle recovery
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{provisioner_port}/", content="Wakeup", timeout=None)
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200
        
        # Provisioner should deallocate the preempted VM and active count should drop to 0
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
        print('     ├─ [Assert] Checking: gsa_resp[\"vms\"][\"active_count\"] == 0')
        assert gsa_resp["vms"]["active_count"] == 0


# ===========================================================================
# 14. Localized Segment Recovery & Retry
# ===========================================================================