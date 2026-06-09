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


def test_effect_pydantic_round_trip():

    print('\n▶️  [STARTING TEST] test_effect_pydantic_round_trip')
    """Create representative effects, serialize to JSON, deserialize back.

    Verifies: model_dump_json → model_validate_json identity for every
    major effect category (script, job, VM, pipeline, reconciliation).
    Intensity: Light
    """
    import json as json_mod
    import uuid

    effects_to_test = [
        UpdateScript(agent="scenario", blocks=[
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                        text="Dopamine drives motivation.", duration_sec=5.0),
        ]),
        QueueJob(agent="audio", job_id="j-001", job_type="tts",
                 scene_num=1, block_id="s1_b1", slot_id="A1:1:s1_b1",
                 params={"voice": "V1"}),
        JobCompleted(agent="provisioner", job_id="j-001",
                     artifact_uri="/tmp/test.wav", duration_sec=5.2,
                     vm_instance_id="vm-42", measurements=[5.1, 5.2, 5.3]),
        VMAllocated(agent="provisioner", instance_id="vm-42", role="tts",
                    offer_id="offer-1", worker_url="http://1.2.3.4:9000",
                    gpu_type="RTX 4090", cost_per_hour=0.45),
        VMDeallocated(agent="provisioner", instance_id="vm-42",
                      reason="job_done", final_cost=0.12, runtime_sec=960.0),
        PipelineStarted(agent="orchestrator"),
        ReconciliationComplete(agent="audio", blocks_total=3, blocks_passed=3,
                               blocks_failed=0, worst_delta_sec=0.1,
                               total_measured_sec=15.0),
        MergeIntoOTIO(agent="video", job_id="j-002", block_id="s1_b1",
                      scene_num=1, slot_id="V1:1:s1_b1",
                      artifact_uri="/tmp/clip.mp4", track_name="V1_Video",
                      duration_sec=5.0),
    ]

    for original in effects_to_test:
        json_str = original.model_dump_json()
        # Verify it's valid JSON
        parsed_dict = json_mod.loads(json_str)
        print('     ├─ [Assert] Checking: parsed_dict[\"kind\"] == original.kind')
        assert parsed_dict["kind"] == original.kind
        # Reconstruct via the KIND_TO_MODEL registry
        model_cls = KIND_TO_MODEL[original.kind]
        rebuilt = model_cls.model_validate_json(json_str)
        print('     ├─ [Assert] Checking: rebuilt.kind == original.kind')
        assert rebuilt.kind == original.kind
        print('     ├─ [Assert] Checking: str(rebuilt.effect_id) == str(original.effect_id)')
        assert str(rebuilt.effect_id) == str(original.effect_id)
        print('     ├─ [Assert] Checking: rebuilt.agent == original.agent')
        assert rebuilt.agent == original.agent

    print(f"    ✓ {len(effects_to_test)} effects round-tripped through JSON")


# ===========================================================================
# 22. EventStore: Append, Replay, and Monotonic Ordering
# ===========================================================================