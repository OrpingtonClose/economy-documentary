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



def test_covering_audio_agent_tts_job_queueing():

    print('\n▶️  [STARTING TEST] test_covering_audio_agent_tts_job_queueing')
    """Verify that Audio Agent scans GSA slots and queues TTS jobs."""
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "audio"]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        audio_port = harness.ports["audio"]
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        # Seed script block in event store
        blocks = [
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator", text="Dopamine drives motivation.", duration_sec=3.0)
        ]
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        event_store.append(UpdateScript(agent="scenario", blocks=blocks), "initial_hash")
        
        # Wake up Audio Agent
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        resp = httpx.post(f"http://127.0.0.1:{audio_port}/", content="Wakeup")
        print('     ├─ [Assert] Checking: resp.status_code == 200')
        assert resp.status_code == 200
        
        # Check GSA to see if TTS job was queued
        print('     ├─ [HTTP] Sending request to agent endpoint...')
        gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/").json()
        jobs = gsa_resp["jobs"]["jobs"]
        print('     ├─ [Assert] Checking: len(jobs) >= 1')
        assert len(jobs) >= 1
        print('     ├─ [Assert] Checking: any(j[\"job_type\"] == \"tts\" for j in jobs.values())')
        assert any(j["job_type"] == "tts" for j in jobs.values())


    # ===========================================================================
    # 4. Video Agent LTX Job Queueing
    # ===========================================================================
