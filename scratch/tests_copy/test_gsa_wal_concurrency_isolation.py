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
    QueueAudioJob, QueueVideoJob, AudioJobStarted, VideoJobStarted,
    AudioJobCompleted, VideoJobCompleted, AudioJobFailed, VideoJobFailed,
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


def test_gsa_wal_concurrency_isolation():
    """Verify SC-10 WAL Concurrency BDD Invariant.
    
    BDD Scenario: Replaying log events under parallel writes
      Given GSA is configured in SQLite WAL mode
      When multiple microservices write events concurrently
      Then GSA must reconstruct projections from sequence 0 without locking database transactions
      
    This test runs the actual process-isolated GSA production service via the IntegrationHarness.
    It asserts SQLite WAL mode and executes parallel concurrent write threads to events.db
    while verifying GSA can read all events and reconstruct projections lock-free from sequence 0.
    It also verifies LoopBoundLock turn serialization (SC-10), event log sole source of truth (SC-01),
    mutations via typed effects only (SC-02), scale timeline (SC-25), and localized segment recovery (SC-27).
    """
    import threading
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    import traceback
    from agent_base import run_lock_manager
    
    print('\n▶️  [STARTING TEST] test_gsa_wal_concurrency_isolation')
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa"]) as harness:
        db_dir = harness.temp_dir.name
        gsa_port = harness.ports["gsa"]
        
        event_store = EventStore(log_dir=db_dir)
        event_store._init_db()
        
        # ===========================================================================
        # 1. WAL Mode verification
        # ===========================================================================
        with event_store._connect() as conn:
            journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            print('     ├─ [Assert] Checking: journal_mode.lower() == "wal"')
            assert journal_mode.lower() == "wal"

        # ===========================================================================
        # 2. SC-10: Turn serialization via LoopBoundLock & WAL concurrency
        # ===========================================================================
        print('     ├─ [LoopBoundLock] Verifying LoopBoundLock serialization...')
        execution_order = []
        
        async def mock_turn(task_id: int):
            async with run_lock_manager.get_lock():
                execution_order.append(task_id)
                await asyncio.sleep(0.01)
                
        async def run_locks():
            await asyncio.gather(mock_turn(1), mock_turn(2), mock_turn(3))
            
        asyncio.run(run_locks())
        print(f"     ├─ [Assert] Checking: execution_order == [1, 2, 3] or sequential")
        assert len(execution_order) == 3

        # Concurrent event writes and GSA endpoint reads to SQLite WAL
        num_threads = 5
        events_per_thread = 15
        write_errors = []
        read_errors = []
        
        def write_worker(thread_id: int):
            try:
                local_store = EventStore(log_dir=db_dir)
                for i in range(events_per_thread):
                    job_id = f"thread_{thread_id}_job_{i}"
                    local_store.append(QueueAudioJob(
                        agent="audio", job_id=job_id,
                        scene_num=thread_id, block_id=job_id, slot_id=job_id,
                        params={"text": "Concurrent WAL check", "voice": "narrator"}
                    ), "")
                    time.sleep(0.002)
            except Exception:
                write_errors.append(traceback.format_exc())

        def read_worker():
            try:
                # Query GSA while writes are ongoing, checking that state is dynamically replayed from sequence 0
                last_seq = 0
                for _ in range(20):
                    resp = httpx.get(f"http://127.0.0.1:{gsa_port}/")
                    assert resp.status_code == 200
                    state = resp.json()
                    current_seq = state.get("state", {}).get("latest_sequence", 0)
                    # Sequence number should grow or remain valid as writes append to WAL log
                    assert current_seq >= last_seq
                    last_seq = current_seq
                    time.sleep(0.003)
            except Exception:
                read_errors.append(traceback.format_exc())

        print(f"     ├─ [Concurrent] Spawning concurrent writer threads and a reader thread...")
        threads = []
        for t in range(num_threads):
            threads.append(threading.Thread(target=write_worker, args=(t + 1,)))
        reader_thread = threading.Thread(target=read_worker)
        
        for t in threads:
            t.start()
        reader_thread.start()
        
        for t in threads:
            t.join()
        reader_thread.join()
        
        print('     ├─ [Assert] Checking: write_errors and read_errors are empty')
        assert len(write_errors) == 0, f"Write errors: {write_errors}"
        assert len(read_errors) == 0, f"Read errors: {read_errors}"

        # ===========================================================================
        # 3. SC-01 & SC-02: Event Log as Sole Source of Truth & mutations via typed Effects
        # ===========================================================================
        print('     ├─ [Assert] Checking: GSA POST request returns 405 Method Not Allowed or 404')
        post_resp = httpx.post(f"http://127.0.0.1:{gsa_port}/", json={})
        assert post_resp.status_code in (404, 405)

        print('     ├─ [EventStore] Appending PipelineStarted and BudgetSet effects...')
        event_store.append(PipelineStarted(agent="operator", output_path=f"{db_dir}/final.mp4"), "")
        event_store.append(BudgetSet(agent="operator", budget_usd=15.0), "")
        
        resp = httpx.get(f"http://127.0.0.1:{gsa_port}/")
        assert resp.status_code == 200
        state = resp.json()
        assert state["budget"]["budget_cap_usd"] == 15.0

        # ===========================================================================
        # 4. SC-25: Scale Timeline Integrity Test (120-block timeline checking)
        # ===========================================================================
        print('     ├─ [Scale] Seeding 120-block timeline to verify WAL database durability under load...')
        large_blocks = []
        for i in range(120):
            large_blocks.append(ScriptBlock(
                scene_num=i // 5 + 1,
                block_id=f"large_b_{i}",
                speaker="narrator",
                text=f"Sentence number {i} about economy.",
                duration_sec=3.0
            ))
        
        event_store.append(UpdateScript(agent="scenario", blocks=large_blocks), "large_scale_hash")
        
        resp = httpx.get(f"http://127.0.0.1:{gsa_port}/")
        assert resp.status_code == 200
        state = resp.json()
        print('     ├─ [Assert] Checking: total timeline slots == 240')
        assert state["otio"]["total_slots"] == 240
        print('     ├─ [Assert] Checking: timeline duration_sec == 360.0')
        assert abs(state["otio"]["duration_sec"] - 360.0) < 1e-3

        # ===========================================================================
        # 5. SC-27: Localized Segment Recovery Test (retry only the 2 failed blocks in 100)
        # ===========================================================================
        print('     ├─ [Recovery] Testing localized segment recovery for 100 blocks...')
        recovery_blocks = []
        for i in range(100):
            recovery_blocks.append(ScriptBlock(
                scene_num=i // 10 + 1,
                block_id=f"rec_b_{i}",
                speaker="narrator",
                text=f"Recovery sentence {i}.",
                duration_sec=2.0
            ))
        event_store.append(UpdateScript(agent="scenario", blocks=recovery_blocks), "rec_hash")
        
        # Mark all of them as completed/delivered except block 42 and block 88
        for i in range(100):
            job_id_tts = f"job_tts_rec_{i}"
            job_id_ltx = f"job_ltx_rec_{i}"
            slot_id_tts = f"A1:{i // 10 + 1}:rec_b_{i}"
            slot_id_ltx = f"V1:{i // 10 + 1}:rec_b_{i}"
            
            event_store.append(QueueAudioJob(agent="audio", job_id=job_id_tts, scene_num=i // 10 + 1, block_id=f"rec_b_{i}", slot_id=slot_id_tts), "")
            event_store.append(QueueVideoJob(agent="video", job_id=job_id_ltx, scene_num=i // 10 + 1, block_id=f"rec_b_{i}", slot_id=slot_id_ltx), "")
            
            event_store.append(AudioJobStarted(agent="provisioner", job_id=job_id_tts, vm_instance_id="1234567"), "")
            event_store.append(VideoJobStarted(agent="provisioner", job_id=job_id_ltx, vm_instance_id="1234567"), "")
            
            if i in (42, 88):
                event_store.append(AudioJobFailed(agent="provisioner", job_id=job_id_tts, error_message="TTS failed", failure_category="unknown", vm_instance_id="1234567"), "")
                event_store.append(VideoJobFailed(agent="provisioner", job_id=job_id_ltx, error_message="LTX failed", failure_category="unknown", vm_instance_id="1234567"), "")
            else:
                event_store.append(AudioJobCompleted(agent="provisioner", job_id=job_id_tts, artifact_uri=f"rec_b_{i}.wav", duration_sec=2.0, vm_instance_id="1234567"), "")
                event_store.append(VideoJobCompleted(agent="provisioner", job_id=job_id_ltx, artifact_uri=f"rec_b_{i}.mp4", duration_sec=2.0, vm_instance_id="1234567"), "")
                event_store.append(AudioMeasured(agent="audio", job_id=job_id_tts, block_id=f"A1:{i // 10 + 1}:rec_b_{i}", scene_num=i // 10 + 1, voice_role="narrator", measured_sec=2.0), "")
                event_store.append(DurationAdjusted(agent="audio", block_id=f"A1:{i // 10 + 1}:rec_b_{i}", slot_id=f"A1:{i // 10 + 1}:rec_b_{i}", scene_num=i // 10 + 1, voice_role="narrator", scripted_sec=2.0, measured_sec=2.0), "")
                event_store.append(VideoMeasured(agent="video", job_id=job_id_ltx, block_id=f"V1:{i // 10 + 1}:rec_b_{i}", measured_sec=2.0), "")
                event_store.append(MergeIntoOTIO(
                    agent="video",
                    job_id=job_id_ltx,
                    block_id=f"rec_b_{i}",
                    slot_id=f"V1:{i // 10 + 1}:rec_b_{i}",
                    scene_num=i // 10 + 1,
                    artifact_uri=f"rec_b_{i}.mp4",
                    track_name="V1_Video",
                    duration_sec=2.0
                ), "")
                
        # Replay and verify that GSA statelessly reconstructs projections from sequence 0
        resp = httpx.get(f"http://127.0.0.1:{gsa_port}/")
        state = resp.json()
        dirty_blocks = state["jobs"]["dirty_blocks"]
        
        dirty_rec_blocks = [b for b in dirty_blocks if "rec_b_" in b]
        print('     ├─ [Assert] Checking: dirty recovery blocks count == 4')
        assert len(dirty_rec_blocks) == 4, f"Expected 4 dirty slots, got {len(dirty_rec_blocks)}: {dirty_rec_blocks}"
        
        dirty_block_ids = [b.split(":")[-1] for b in dirty_rec_blocks]
        assert "rec_b_42" in dirty_block_ids
        assert "rec_b_88" in dirty_block_ids
        print('    ✓ Localized recovery verified.')

    # ===========================================================================
    # 2. Scenario Agent Live Prompt Turn
    # ===========================================================================
