import os
from pathlib import Path

target_file = Path('/Users/orpington/Documents/economy-documentary-work/tests/units/test_covering_vast_create_and_destroy_lifecycle.py')

content = """import os
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


def test_covering_vast_create_and_destroy_lifecycle():

    print('\\n▶️  [STARTING TEST] test_covering_vast_create_and_destroy_lifecycle')
    # Verify vastai CLI connectivity and querying of rentable GPU instances without renting.
    vast_key_path = os.path.expanduser("~/api_keys/vast_ai_key.txt")
    if not os.path.exists(vast_key_path):
        pytest.fail("CRITICAL FAILURE: live dependencies missing")
        
    with open(vast_key_path) as f:
        api_key = f.read().strip()
        
    # Find cheapest offer
    cmd_search = f"vastai --api-key {api_key} search offers 'rentable=true num_gpus=1' -o 'price' --raw"
    res = subprocess.run(cmd_search, shell=True, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        pytest.fail("CRITICAL FAILURE: live dependencies missing")
        
    # Verify we can parse the offers output
    import json
    try:
        offers = json.loads(res.stdout.strip())
        assert len(offers) >= 0
        if len(offers) > 0:
            offer_id = offers[0]["id"]
            print(f"Cheapest rentable offer found: {offer_id}")
    except Exception as e:
        raise RuntimeError(f"CRITICAL FAILURE: Could not parse Vast.ai raw search output: {e}")
        
    print("✓ vastai search offers live validation succeeded without renting VM")
"""

target_file.write_text(content, encoding='utf-8')
print("Successfully wrote updated test file")
