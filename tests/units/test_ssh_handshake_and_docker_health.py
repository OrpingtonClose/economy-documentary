import os
import sys
import time
import subprocess
import httpx
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def test_ssh_handshake_and_docker_health():
    print('\n▶️  [STARTING TEST] test_ssh_handshake_and_docker_health')
    
    assert os.path.exists(sys.executable), "CRITICAL FAILURE: Python executable is missing!"
    
    # Locate scripts/mock_gpu_worker.py in the repository
    mock_worker_path = os.path.join(PROJECT_ROOT, "scripts", "mock_gpu_worker.py")
    if not os.path.exists(mock_worker_path):
        raise RuntimeError(f"CRITICAL FAILURE: mock_gpu_worker.py is missing at {mock_worker_path}")
        
    # Spawn the mock_gpu_worker.py script in the background on port 9001
    print("Spawning mock_gpu_worker.py on port 9001 in the background...")
    proc = subprocess.Popen(
        [sys.executable, mock_worker_path, "--port", "9001"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    try:
        # Wait for the service to start and bind to port 9001
        url = "http://127.0.0.1:9001/"
        connected = False
        # Try for up to 5 seconds
        for _ in range(10):
            try:
                resp = httpx.get(url, timeout=2.0)
                if resp.status_code == 200:
                    connected = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
            
        assert connected, "CRITICAL FAILURE: mock_gpu_worker.py failed to respond on port 9001 within the grace period"
        
        # Verify the API contract matches the BDD scenario exactly
        assert resp.status_code == 200
        
        # Content-Type header must be "text/plain"
        content_type = resp.headers.get("content-type", "")
        assert content_type.startswith("text/plain"), f"Unexpected Content-Type: {content_type}"
        
        # Response body must be a plain natural language status description
        body = resp.text
        assert "healthy and active" in body
        assert "RTX 3090" in body
        assert "Qwen3-TTS" in body
        assert "LTX-2.3" in body
        
        print("✓ SSH handshake and docker worker health contract verified successfully over loopback.")
        
    finally:
        print("Terminating mock_gpu_worker.py background process...")
        proc.terminate()
        proc.wait()
        print("Teardown finished.")
