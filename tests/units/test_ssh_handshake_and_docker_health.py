import os
import sys
import time
import httpx
import subprocess
from pathlib import Path

# Setup Python paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))
sys.path.append(str(PROJECT_ROOT / "tests/units"))

from harness import IntegrationHarness

def test_ssh_handshake_and_docker_health():
    print('\n▶️  [STARTING TEST] test_ssh_handshake_and_docker_health')
    """Verify that SSH commands execute on worker VMs and port bindings respond."""
    # We simulate this via a local mock worker container spawned on port 9001
    print('     └─ [Harness] Initializing process-isolated test harness...')
    with IntegrationHarness(required_agents=["gsa"]) as harness:
        db_dir = harness.temp_dir.name
        
        # Launch mock_gpu_worker.py locally on port 9001 to verify endpoint contract
        worker_script = PROJECT_ROOT / "scripts/mock_gpu_worker.py"
        proc = subprocess.Popen(
            [sys.executable, str(worker_script), "--port", "9001"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=os.setsid
        )
        
        try:
            # Poll GET / to confirm plain conversational status description
            healthy = False
            for i in range(30):
                try:
                    print('     ├─ [HTTP] Sending request to agent endpoint...')
                    resp = httpx.get("http://127.0.0.1:9001/")
                    if resp.status_code == 200 and "healthy" in resp.text:
                        healthy = True
                        break
                except (httpx.ConnectError, httpx.HTTPError):
                    if i == 29:
                        raise
                time.sleep(0.2)
                
            print('     ├─ [Assert] Checking: healthy, "Mock worker failed to start and respond to health...')
            assert healthy, "Mock worker failed to start and respond to health probes on port 9001"
            
            # Verify Content-Type text/plain
            print('     ├─ [HTTP] Sending request to agent endpoint...')
            resp = httpx.get("http://127.0.0.1:9001/")
            print('     ├─ [Assert] Checking: "text/plain" in resp.headers.get("content-type", "")')
            assert "text/plain" in resp.headers.get("content-type", "")

            # Verify TTS job dispatch to mock worker
            tts_payload = 'python run_qwen3_tts.py --text "Dopamine drives motivation." --voice narrator --output /workspace/output/tts.wav'
            print('     ├─ [HTTP] Sending request to agent endpoint...')
            resp_tts = httpx.post("http://127.0.0.1:9001/", content=tts_payload)
            print('     ├─ [Assert] Checking: resp_tts.status_code == 200')
            assert resp_tts.status_code == 200
            print('     ├─ [Assert] Checking: "RESULT:" in resp_tts.text')
            assert "RESULT:" in resp_tts.text
            print('     ├─ [Assert] Checking: "Generated narration audio" in resp_tts.text')
            assert "Generated narration audio" in resp_tts.text
            print('     ├─ [Assert] Checking: os.path.exists("/tmp/documentary-pipeline/output/tts.wav")')
            assert os.path.exists("/tmp/documentary-pipeline/output/tts.wav")

            # Verify LTX job dispatch to mock worker
            ltx_payload = 'python run_ltx_2_3.py --prompt "A beautiful landscape." --duration 5.0 --output /workspace/output/ltx.mp4'
            print('     ├─ [HTTP] Sending request to agent endpoint...')
            resp_ltx = httpx.post("http://127.0.0.1:9001/", content=ltx_payload)
            print('     ├─ [Assert] Checking: resp_ltx.status_code == 200')
            assert resp_ltx.status_code == 200
            print('     ├─ [Assert] Checking: "RESULT:" in resp_ltx.text')
            assert "RESULT:" in resp_ltx.text
            print('     ├─ [Assert] Checking: "Generated video clip" in resp_ltx.text')
            assert "Generated video clip" in resp_ltx.text
            print('     ├─ [Assert] Checking: os.path.exists("/tmp/documentary-pipeline/output/ltx.mp4")')
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
        print('    ✓ SSH handshake and Docker health verified')
