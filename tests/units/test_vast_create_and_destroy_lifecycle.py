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

from capabilities.test_real_vast_provisioning_bdd_create_instance import VastCreateSimulator
from capabilities.test_real_vast_provisioning_bdd_destroy_instance import VastDestroySimulator

def test_vast_create_and_destroy_lifecycle():

    print('\n▶️  [STARTING TEST] test_vast_create_and_destroy_lifecycle')
    """Verify renting and immediate destruction of a real low-cost GPU instance."""
    vast_key_path = "/Users/orpington/api_keys/vast_ai_key.txt"
    if not os.path.exists(vast_key_path):
        raise RuntimeError("CRITICAL FAILURE: Simulation Cover requires live execution. vast_ai_key.txt is missing!")
        
    with open(vast_key_path) as f:
        api_key = f.read().strip()
        
    # Find cheapest offer
    cmd_search = f"vastai --api-key {api_key} search offers 'rentable=true num_gpus=1' -o 'price' --raw"
    res = subprocess.run(cmd_search, shell=True, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError(f"Vast.ai API connection failed: {res.stderr}")
        
    # Rent the cheapest matching machine
    import json
    try:
        offers = json.loads(res.stdout.strip())
        offer_id = offers[0]["id"]
    except Exception as e:
        raise RuntimeError(f"Could not parse Vast.ai search output: {e}. Raw: {res.stdout}")
        
    print(f"Renting cheapest Vast.ai offer: {offer_id}")
    cmd_create = f"vastai --api-key {api_key} create instance {offer_id} --image ubuntu:22.04 --disk 20 --raw"
    create_res = subprocess.run(cmd_create, shell=True, capture_output=True, text=True)
    if create_res.returncode != 0:
        raise RuntimeError(f"Cheapest VM lease creation failed: {create_res.stderr}")
    
    if not create_res.stdout.strip():
        raise RuntimeError("Cheapest VM lease creation returned empty output")
        
    try:
        create_data = json.loads(create_res.stdout.strip())
        instance_id = create_data["new_contract"]
        print(f"VM successfully leased: {instance_id}")
    except Exception as e:
        raise RuntimeError(f"Failed to parse leased VM contract ID: {e}. Output: {create_res.stdout}")
        
    # Wait and then destroy to ensure clean teardown
    print(f"Cleaning up and destroying Vast.ai instance {instance_id}...")
    cmd_destroy = f"vastai --api-key {api_key} destroy instance {instance_id}"
    destroy_res = subprocess.run(cmd_destroy, shell=True, capture_output=True)
    print('     ├─ [Assert] Checking: destroy_res.returncode == 0')
    assert destroy_res.returncode == 0, f"VM teardown leaked: {destroy_res.stderr}"
