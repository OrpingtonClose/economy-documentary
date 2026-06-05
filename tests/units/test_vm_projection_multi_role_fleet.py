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


def test_vm_projection_multi_role_fleet():

    print('\n▶️  [STARTING TEST] test_vm_projection_multi_role_fleet')
    """Allocate TTS + LTX VMs, deallocate one, verify active counts by role.

    Exercises the VMs projection's role-filtered queries and cost tracking.
    Intensity: Medium
    """
    vms = VMs()

    # Allocate 2 TTS VMs and 1 LTX VM
    vms.apply(VMAllocated(agent="provisioner", instance_id="vm-tts-1",
                          role="tts", offer_id="o1",
                          worker_url="http://1.1.1.1:9000",
                          gpu_type="RTX 4090", cost_per_hour=0.40))
    vms.apply(VMAllocated(agent="provisioner", instance_id="vm-tts-2",
                          role="tts", offer_id="o2",
                          worker_url="http://2.2.2.2:9000",
                          gpu_type="RTX 4090", cost_per_hour=0.45))
    vms.apply(VMAllocated(agent="provisioner", instance_id="vm-ltx-1",
                          role="ltx", offer_id="o3",
                          worker_url="http://3.3.3.3:9000",
                          gpu_type="H100", cost_per_hour=2.50))

    print('     ├─ [Assert] Checking: len(vms.active_vms()) == 3')
    assert len(vms.active_vms()) == 3
    print('     ├─ [Assert] Checking: len(vms.active_vms(\"tts\")) == 2')
    assert len(vms.active_vms("tts")) == 2
    print('     ├─ [Assert] Checking: len(vms.active_vms(\"ltx\")) == 1')
    assert len(vms.active_vms("ltx")) == 1
    print('     ├─ [Assert] Checking: abs(vms.estimated_hourly_cost() - (0.40 + 0.45 + 2.50)) < 0....')
    assert abs(vms.estimated_hourly_cost() - (0.40 + 0.45 + 2.50)) < 0.01

    # Deallocate one TTS VM
    vms.apply(VMDeallocated(agent="provisioner", instance_id="vm-tts-1",
                            reason="job_done", final_cost=0.10))

    print('     ├─ [Assert] Checking: len(vms.active_vms()) == 2')
    assert len(vms.active_vms()) == 2
    print('     ├─ [Assert] Checking: len(vms.active_vms(\"tts\")) == 1')
    assert len(vms.active_vms("tts")) == 1
    print('     ├─ [Assert] Checking: vms.vms[\"vm-tts-1\"].status == \"destroyed\"')
    assert vms.vms["vm-tts-1"].status == "destroyed"

    # Observe drift on LTX VM
    vms.apply(VMObserved(agent="provisioner", instance_id="vm-ltx-1",
                         observed_status="not_found", expected_status="running",
                         drift_description="VM disappeared from Vast.ai"))

    print('     ├─ [Assert] Checking: vms.vms[\"vm-ltx-1\"].status == \"observed_gone\"')
    assert vms.vms["vm-ltx-1"].status == "observed_gone"
    print('     ├─ [Assert] Checking: vms.vms[\"vm-ltx-1\"].observed_status == \"not_found\"')
    assert vms.vms["vm-ltx-1"].observed_status == "not_found"
    print('     ├─ [Assert] Checking: len(vms.active_vms()) == 1  # only vm-tts-2 is truly active')
    assert len(vms.active_vms()) == 1  # only vm-tts-2 is truly active

    # Summary should reflect state
    summary = vms.summary()
    print('     ├─ [Assert] Checking: \"1/\" in summary  # 1 active out of total')
    assert "1/" in summary  # 1 active out of total

    print("    ✓ 3-VM fleet: allocate, deallocate, drift → correct counts")


# ===========================================================================
# 33. BudgetProjection: Exceeded Detection
# ===========================================================================