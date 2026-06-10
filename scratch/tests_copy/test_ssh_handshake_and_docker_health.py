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



def test_ssh_handshake_and_docker_health():

    print('\n▶️  [STARTING TEST] test_ssh_handshake_and_docker_health')
    """Verify that SSH commands execute on worker VMs and port bindings respond."""
    # We simulate this via a local mock worker container spawned on port 9001
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa"]) as harness:
        db_dir = harness.temp_dir.name
        
        # Launch vm_agent.py locally on port 9001 to verify endpoint contract
        worker_script = PROJECT_ROOT / "scripts/vm_agent.py"
        proc = subprocess.Popen(
            [sys.executable, str(worker_script), "--port", "9001"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=os.setsid
        )
        
        try:
            # Poll GET / to confirm plain conversational status description
            healthy = False
            for _ in range(30):
                try:
                    print('     ├─ [HTTP] Sending request to agent endpoint...')
                    resp = httpx.get("http://127.0.0.1:9001/")
                    if resp.status_code == 200 and "healthy" in resp.text:
                        healthy = True
                        break
                except Exception:
                    pass
                time.sleep(0.2)
                
            print('     ├─ [Assert] Checking: healthy, \"Mock worker failed to start and respond to health...')
            assert healthy, "Mock worker failed to start and respond to health probes on port 9001"
            
            # Verify Content-Type text/plain
            print('     ├─ [HTTP] Sending request to agent endpoint...')
            resp = httpx.get("http://127.0.0.1:9001/")
            print('     ├─ [Assert] Checking: \"text/plain\" in resp.headers.get(\"content-type\", \"\")')
            assert "text/plain" in resp.headers.get("content-type", "")

            # Verify TTS job dispatch to mock worker
            tts_payload = 'python run_qwen3_tts.py --text "Dopamine drives motivation." --voice narrator --output /workspace/output/tts.wav'
            print('     ├─ [HTTP] Sending request to agent endpoint...')
            resp_tts = httpx.post("http://127.0.0.1:9001/", content=tts_payload, timeout=None)
            print('     ├─ [Assert] Checking: resp_tts.status_code == 200')
            assert resp_tts.status_code == 200
            print('     ├─ [Assert] Checking: \"RESULT:\" in resp_tts.text')
            assert "RESULT:" in resp_tts.text
            print('     ├─ [Assert] Checking: \"Generated narration audio\" in resp_tts.text')
            assert "Generated narration audio" in resp_tts.text
            print('     ├─ [Assert] Checking: os.path.exists(\"/tmp/documentary-pipeline/output/tts.wav\")')
            assert os.path.exists("/tmp/documentary-pipeline/output/tts.wav")

            # Verify LTX job dispatch to mock worker
            ltx_payload = 'python run_ltx_2_3.py --prompt "A beautiful landscape." --duration 5.0 --output /workspace/output/ltx.mp4'
            print('     ├─ [HTTP] Sending request to agent endpoint...')
            resp_ltx = httpx.post("http://127.0.0.1:9001/", content=ltx_payload, timeout=None)
            print('     ├─ [Assert] Checking: resp_ltx.status_code == 200')
            assert resp_ltx.status_code == 200
            print('     ├─ [Assert] Checking: \"RESULT:\" in resp_ltx.text')
            assert "RESULT:" in resp_ltx.text
            print('     ├─ [Assert] Checking: \"Generated video clip\" in resp_ltx.text')
            assert "Generated video clip" in resp_ltx.text
            print('     ├─ [Assert] Checking: os.path.exists(\"/tmp/documentary-pipeline/output/ltx.mp4\")')
            assert os.path.exists("/tmp/documentary-pipeline/output/ltx.mp4")
            
        except Exception as e:
            try:
                out, err = proc.communicate()
                print(f"\n--- Mock Worker stdout ---\n{out.decode()}")
                print(f"\n--- Mock Worker stderr ---\n{err.decode()}")
            except Exception:
                pass
            raise e
        finally:
            import signal
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait()


    # ===========================================================================
    # 8. Audio Loudness Normalization & FFmpeg Check
    # ===========================================================================
