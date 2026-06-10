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



def test_simulation_provisioner_escalation_policy():

    print('\n▶️  [STARTING TEST] test_provisioner_escalation_policy')
    """Verify that the Provisioner Agent starts by allocating exactly 1 VM under a pending queue backlog."""
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "provisioner"]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        provisioner_port = harness.ports["provisioner"]
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        
        # Seed 5 pending jobs
        for i in range(1, 6):
            print('     ├─ [EventStore] Appending event to SQLite events database...')
            event_store.append(QueueJob(
                agent="audio", job_id=f"job_tts_{i}", job_type="tts",
                scene_num=1, block_id=f"s1_b{i}", slot_id=f"A1:1:s1_b{i}",
                params={"text": f"Hello {i}", "voice": "narrator", "gpu_type": "RTX 4090"}
            ), "")
            
        # Wake up Provisioner Agent
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{provisioner_port}/", content="Wakeup")
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200
        
        # Provisioner should search and allocate exactly 1 VM (the initial VM to verify happy-path)
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
        vms = gsa_resp["vms"]["vms"]
        print('     ├─ [Assert] Checking: len(vms) == 1')
        assert len(vms) == 1


# ===========================================================================
# 13. Infrastructure Preemption and Failure Recovery
# ===========================================================================