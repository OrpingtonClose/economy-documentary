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

from capabilities.test_single_purpose_tts_simulators import TtsMultiBlockSimulator

def test_simulation_bdd_multi_block_tts_reconciliation():

    print('\n▶️  [STARTING TEST] test_simulation_bdd_multi_block_tts_reconciliation')
    """Given a TTS VM is active and 3 TTS jobs are pending,
    When the audio agent dispatches all 3,
    Then all WAVs are produced and durations are measured.

    BDD judge evaluates: consistency of durations, reconciliation completeness.
    Intensity: Heavy (real harness, 3 agents)
    """
    scenario = BddScenario(
        test_name="multi_block_tts_reconciliation",
        given="TTS VM active, 3 pending TTS jobs for blocks s1_b1, s1_b2, s2_b1",
        when="Audio agent dispatches all 3 jobs in sequence",
        then="3 WAV files produced, all with plausible durations, "
             "ReconciliationComplete or equivalent events emitted",
    )
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa", "audio", "provisioner"]) as harness:
        db_dir = harness.temp_dir.name
        audio_port = harness.ports["audio"]

        store = EventStore(log_dir=db_dir)
        store._init_db()

        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(BudgetSet(agent="operator", budget_usd=5.0), "")
        blocks = [
            ScriptBlock(scene_num=1, block_id="s1_b1", speaker="narrator",
                        text="Interest rates affect housing.", duration_sec=3.0),
            ScriptBlock(scene_num=1, block_id="s1_b2", speaker="narrator",
                        text="Mortgages become more expensive.", duration_sec=3.5),
            ScriptBlock(scene_num=2, block_id="s2_b1", speaker="narrator",
                        text="Consumer spending contracts.", duration_sec=4.0),
        ]
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(UpdateScript(agent="scenario", blocks=blocks), "init")
        print('     ├─ [EventStore] Appending event to SQLite events database...')
        store.append(VMAllocated(
            agent="provisioner", instance_id="1234567", role="tts",
            offer_id="101", worker_url="http://127.0.0.1:9001",
            gpu_type="RTX 4090", cost_per_hour=0.85,
        ), "init")
        for i, blk in enumerate(blocks):
            print('     ├─ [EventStore] Appending event to SQLite events database...')
            store.append(QueueJob(
                agent="audio", job_id=f"job_tts_multi_{i+1}", job_type="tts",
                scene_num=blk.scene_num, block_id=blk.block_id,
                slot_id=f"A1:{blk.scene_num}:{blk.block_id}",
                params={"text": blk.text, "voice": "narrator", "gpu_type": "RTX 4090"},
            ), "init")

        # Action: wake audio agent multiple times to process queue
        for _ in range(4):
            print('     ├─ [HTTP] Sending request to agent endpoint...')
            resp = httpx.post(f"http://127.0.0.1:{audio_port}/", content="Wakeup", timeout=None)
            print('     ├─ [Assert] Checking: resp.status_code == 200')
            assert resp.status_code == 200

        # Mechanical: count completed jobs
        events = store.replay()
        completed = [e for e in events if e.effect.kind == "job_completed"]

        # Collect WAV artifacts
        audio_dir = os.path.join(db_dir, "audio_outputs")
        wav_files = list(Path(audio_dir).glob("*.wav")) if os.path.isdir(audio_dir) else []
        artifacts = {"wav_count": len(wav_files), "completed_count": len(completed)}

        scenario.evidence = collect_evidence_from_store(events, artifacts=artifacts)
        print('     ├─ [BDD Judge] Executing LLM BDD Judge validation...')
        verdict = asyncio.run(run_bdd_judge(scenario, db_dir))
        print('     ├─ [Assert] Checking: verdict[\"verdict\"] != \"fail\", f\"BDD judge failed: {verd...')
        assert verdict["verdict"] != "fail", f"BDD judge failed: {verdict['reasoning']}"
        print(f"    ✓ multi-block TTS reconciliation — verdict: {verdict['verdict']}")


# ===========================================================================
# 39. BDD: Voice Continuity Across Scenes — spectral consistency
# ===========================================================================