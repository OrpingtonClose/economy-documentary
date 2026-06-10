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



def test_covering_perplexity_verify_live():

    print('\n▶️  [STARTING TEST] test_covering_perplexity_verify_live')
    """Verify Perplexity API fact-checking tool using real credentials."""
    import os
    import asyncio
    
    # 1. Load and inject API key into environment BEFORE importing tools module
    key_path = os.path.expanduser("~/api_keys/LLMS/perplexity_api_key.txt")
    if os.path.exists(key_path) and not os.environ.get("PERPLEXITY_API_KEY"):
        with open(key_path) as f:
            os.environ["PERPLEXITY_API_KEY"] = f.read().strip()
            
    # 2. Import the real tool from pipeline.swarm_extraction.tools (resolves key at import time)
    from pipeline.swarm_extraction.tools import perplexity_verify
    
    # 3. Run fact-checking query
    claim = "The capital city of France is Paris."
    print(f"     ├─ [API] Sending live query to Perplexity Sonar Pro for claim: '{claim}'")
    result = asyncio.run(perplexity_verify(claim))
    print(f"     ├─ [API] Received response: {result}")
    
    # 4. Asserts
    print('     ├─ [Assert] Checking: not result.startswith(\"[TOOL_ERROR]\")')
    assert not result.startswith("[TOOL_ERROR]"), f"Perplexity API returned tool error: {result}"
    print('     ├─ [Assert] Checking: \"VERIFIED\" in result or \"TRUE\" in result or \"Paris\" in result')
    assert "VERIFIED" in result or "TRUE" in result or "Paris" in result, f"Expected verification statement, got: {result}"
    print('     ├─ [Assert] Checking: \"Sources:\" in result')
    assert "Sources:" in result, f"Expected citation sources, got: {result}"
    print('    ✓ perplexity verify live passed')
