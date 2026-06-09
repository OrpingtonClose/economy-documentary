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



def test_simulation_localized_recovery_and_retry():

    print('\n▶️  [STARTING TEST] test_localized_recovery_and_retry')
    """Verify that Provisioner Agent triggers retry/recovery for failed jobs."""
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "provisioner"]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        provisioner_port = harness.ports["provisioner"]
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        
        # Seed a failed job
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(QueueJob(
            agent="audio", job_id="job_tts_fail", job_type="tts",
            scene_num=1, block_id="s1_b1", slot_id="A1:1:s1_b1",
            params={"text": "Fail test", "voice": "narrator", "gpu_type": "RTX 4090"}
        ), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(JobFailed(
            agent="provisioner", job_id="job_tts_fail",
            error_message="CUDA out of memory", failure_category="oom",
            vm_instance_id="1234567"
        ), "")
        
        # Wake up Provisioner to handle recovery / retry
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{provisioner_port}/", content="Wakeup")
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200
        
        # Provisioner should see the pending/failed job and allocate a VM or retry it
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
        print('     ├─ [Assert] Checking: len(gsa_resp[\"vms\"][\"vms\"]) >= 1 or any(j[\"status\"] ==...')
        assert len(gsa_resp["vms"]["vms"]) >= 1 or any(j["status"] == "pending" for j in gsa_resp["jobs"]["jobs"].values())


# ===========================================================================
# 15. Accumulative Duration Drift Correction & Final Assembly Cover
# ===========================================================================