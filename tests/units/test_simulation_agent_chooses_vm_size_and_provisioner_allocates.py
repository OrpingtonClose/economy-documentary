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


# BDD judge imports
sys.path.append(str(PROJECT_ROOT / "server" / "capabilities"))



def test_simulation_agent_chooses_vm_size_and_provisioner_allocates():

    print('\n▶️  [STARTING TEST] test_agent_chooses_vm_size_and_provisioner_allocates')
    """Verify that Audio/Video agents specify gpu_type, and Provisioner reads it."""
    from effects import AudioMeasured
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "audio", "video", "provisioner"]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        audio_port = harness.ports["audio"]
        video_port = harness.ports["video"]
        provisioner_port = harness.ports["provisioner"]

        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()

        # Seed initial pipeline start and script update
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        blocks = [
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="Dopamine drives motivation.", duration_sec=3.0)
        ]
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")

        # 1. Wake up Audio Agent and verify it queues job specifying the VM/GPU size
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{audio_port}/", content="Wakeup")
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200

        print('     ├─ [HTTP] Sending request to agent endpoint...')
        gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
        jobs = gsa_resp["jobs"]["jobs"]
        tts_job = next(j for j in jobs.values() if j["job_type"] == "tts")
        # Assert chosen gpu_type is specified in job params
        print('     ├─ [Assert] Checking: tts_job[\"params\"].get(\"gpu_type\") == \"RTX 4090\"')
        assert tts_job["params"].get("gpu_type") == "RTX 4090"

        # 2. Wake up Provisioner and verify it reads the chosen gpu_type and allocates the VM
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{provisioner_port}/", content="Wakeup")
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200

        # Wait for the TTS VM to be allocated (avoiding race conditions with background loops)
        allocated_vm = None
        for _ in range(50):
            gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
            vms = gsa_resp.get("vms", {}).get("vms", {})
            try:
                allocated_vm = next(v for v in vms.values() if v["role"] == "tts")
                break
            except StopIteration:
                pass
            time.sleep(0.1)
        assert allocated_vm is not None, "TTS VM was not allocated"
        # Provisioner should allocate with the specified gpu_type from the job
        print('     ├─ [Assert] Checking: allocated_vm[\"gpu_type\"] == \"RTX 4090\"')
        assert allocated_vm["gpu_type"] == "RTX 4090"

        # Simulate completion of TTS job and measurement approval for video agent test
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(JobStarted(agent="provisioner", job_id=tts_job["job_id"], vm_instance_id="1234567"), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(JobCompleted(agent="provisioner", job_id=tts_job["job_id"], artifact_uri="mock.wav", duration_sec=3.0, vm_instance_id="1234567"), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(AudioMeasured(agent="audio", job_id=tts_job["job_id"], block_id="A1:1:s1_b1", scene_num=1, voice_role="narrator", measured_sec=3.0), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(DurationAdjusted(agent="audio", block_id="A1:1:s1_b1", slot_id="A1:1:s1_b1", scene_num=1, voice_role="narrator", scripted_sec=3.0, measured_sec=3.0), "")

        # 3. Wake up Video Agent and verify it queues job specifying the VM/GPU size for LTX
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{video_port}/", content="Wakeup")
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200

        print('     ├─ [HTTP] Sending request to agent endpoint...')
        gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
        jobs = gsa_resp["jobs"]["jobs"]
        ltx_job = next(j for j in jobs.values() if j["job_type"] == "ltx")
        # Assert chosen gpu_type is specified in job params
        print('     ├─ [Assert] Checking: ltx_job[\"params\"].get(\"gpu_type\") == \"RTX A6000\"')
        assert ltx_job["params"].get("gpu_type") == "RTX A6000"

        # 4. Deallocate active TTS VM and run Provisioner to allocate matching LTX VM
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(VMDeallocated(agent="provisioner", instance_id="1234567", reason="stale"), "")
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{provisioner_port}/", content="Wakeup")
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200

        # Wait for the LTX VM to be allocated (avoiding race conditions with background loops)
        allocated_ltx_vm = None
        for _ in range(50):
            gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
            vms = gsa_resp.get("vms", {}).get("vms", {})
            try:
                allocated_ltx_vm = next(v for v in vms.values() if v["role"] == "ltx" and v["status"] == "active")
                break
            except StopIteration:
                pass
            time.sleep(0.1)
        assert allocated_ltx_vm is not None, "LTX VM was not allocated"
        print('     ├─ [Assert] Checking: allocated_ltx_vm[\"gpu_type\"] == \"RTX A6000\"')
        assert allocated_ltx_vm["gpu_type"] == "RTX A6000"


# ===========================================================================
# 12. VM Escalation Policy Verification
# ===========================================================================