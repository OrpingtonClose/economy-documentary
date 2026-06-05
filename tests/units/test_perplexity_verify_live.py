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

from capabilities.test_real_perplexity_verify import PerplexityVerifySimulator

def test_perplexity_verify_live():

    print('\n▶️  [STARTING TEST] test_perplexity_verify_live')
    """Verify Perplexity API fact-checking tool using real credentials."""
    import os
    import pytest
    import asyncio
    
    # 1. Load and inject API key into environment BEFORE importing tools module
    key_path = "/Users/orpington/api_keys/LLMS/perplexity_api_key.txt"
    if not os.path.exists(key_path) and not os.environ.get("PERPLEXITY_API_KEY"):
        pytest.skip("Perplexity API key is missing. Skipping live fact-checking test.")
        
    if os.path.exists(key_path) and not os.environ.get("PERPLEXITY_API_KEY"):
        with open(key_path) as f:
            os.environ["PERPLEXITY_API_KEY"] = f.read().strip()

    # Check network reachability for perplexity API
    import socket
    try:
        socket.setdefaulttimeout(2.0)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("api.perplexity.ai", 443))
    except Exception:
        pytest.skip("api.perplexity.ai is unreachable (offline/restricted network). Skipping live fact-checking test.")
            
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

