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


def test_covering_scenario_agent_live_prompt_turn():

    print('\n▶️  [STARTING TEST] test_covering_scenario_agent_live_prompt_turn')
    deepseek_key_path = os.path.expanduser("~/api_keys/LLMS/deepseek_api.txt")
    if not os.path.exists(deepseek_key_path):
        pytest.fail("CRITICAL FAILURE: live dependencies missing")

    # Check network reachability for deepseek API
    try:
        httpx.get("https://api.deepseek.com/", timeout=None)
    except Exception as e:
        pytest.fail(f"CRITICAL FAILURE: DeepSeek API endpoint is unreachable: {e}")

    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "scenario"], capabilities=[]) as harness:
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
        gsa_resp = httpx.get(f"http://127.0.0.1:{gsa_port}/", timeout=None).json()
        print('     ├─ [Assert] Checking: len(gsa_resp[\"otio\"][\"slots\"]) >= 1')
        assert len(gsa_resp["otio"]["slots"]) >= 1
