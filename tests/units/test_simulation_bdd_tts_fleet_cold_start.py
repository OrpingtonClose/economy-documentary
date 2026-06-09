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

from capabilities.test_single_purpose_tts_simulators import TtsColdStartSimulator

def test_simulation_bdd_tts_fleet_cold_start():

    print('\n▶️  [STARTING TEST] test_simulation_bdd_tts_fleet_cold_start')
    """Given no VMs exist and TTS jobs are queued,
    When the provisioner wakes up,
    Then it searches offers, creates an instance, and copies model weights.

    BDD judge evaluates: real CLI invocations, event sequence completeness,
    and plausible timing for cloud operations.
    Intensity: Heavy (real harness, 2 agents)
    """
    scenario = BddScenario(
        test_name="tts_fleet_cold_start",
        given="Pipeline started with 1 TTS job queued, zero VMs in fleet",
        when="Provisioner agent is woken up via HTTP POST",
        then="Provisioner searches Vast.ai offers, creates a VM instance, "
             "copies model weights, and emits VMAllocated event",
    )
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "provisioner"]) as harness:
        db_dir = harness.temp_dir.name
        provisioner_port = harness.ports["provisioner"]
        gsa_port = harness.ports["gsa"]

        store = EventStore(log_dir=db_dir)
        store._init_db()

        # Precondition: pipeline started, 1 TTS job pending, zero VMs
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(BudgetSet(agent="operator", budget_usd=5.0), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(UpdateScript(agent="scenario", blocks=[
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                        text="Global interest rates shape economic policy.", duration_sec=4.0),
        ]), "init")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(QueueJob(
            agent="audio", job_id="job_tts_cold_1", job_type="tts",
            scene_num=1, block_id="s1_b1", slot_id="A1:1:s1_b1",
            params={"text": "Global interest rates shape economic policy.",
                    "voice": "narrator", "gpu_type": "RTX 4090"},
        ), "init")

        # Action: wait for background provisioner loop to allocate VM
        print('     ├─ [Polling] Waiting for background provisioner to allocate VM...')
        vm_allocated = False
        for _ in range(100):
            try:
                gsa_state = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
                vms = gsa_state.get("vms", {}).get("vms", {})
                if any(v.get("status") == "active" for v in vms.values()):
                    vm_allocated = True
                    break
            except Exception:
                pass
            time.sleep(0.1)
        assert vm_allocated, "Provisioner did not allocate VM in background"


        # Mechanical assertions
        log_path = os.path.join(db_dir, "vastai_invocations.log")
        print('     ├─ [Assert] Checking: os.path.exists(log_path), \"Vast.ai CLI invocation log missi...')
        assert os.path.exists(log_path), "Vast.ai CLI invocation log missing"
        with open(log_path) as f:
            invocations = f.read().splitlines()
        print('     ├─ [Assert] Checking: any(\"search offers\" in cmd for cmd in invocations), \"No s...')
        assert any("search offers" in cmd for cmd in invocations), "No search offers command"
        print('     ├─ [Assert] Checking: any(\"create instance\" in cmd for cmd in invocations), \"No...')
        assert any("create instance" in cmd for cmd in invocations), "No create instance command"

        # Check GSA for VM allocation
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        gsa_state = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
        vms = gsa_state.get("vms", {}).get("vms", {})

        # BDD judge
        scenario.evidence = collect_evidence_from_store(
            store,
            projections={"vms": vms, "invocations": invocations},
            artifacts={"invocation_count": len(invocations)},
        )
        print('     ├─ [BDD Judge] Executing LLM BDD Judge validation...')
        verdict = asyncio.run(run_bdd_judge(scenario, db_dir))
        print('     ├─ [Assert] Checking: verdict[\"verdict\"] != \"fail\", f\"BDD judge failed: {verd...')
        assert verdict["verdict"] != "fail", f"BDD judge failed: {verdict['reasoning']}"
        print(f"    ✓ TTS fleet cold start — verdict: {verdict['verdict']} "
              f"(confidence: {verdict.get('confidence', '?')})")


# ===========================================================================
# 37. BDD: Single Block TTS Inference — audio agent dispatches to GPU
# ===========================================================================