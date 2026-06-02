import os
import sys
import time
import subprocess
import httpx
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

def start_server(name: str, port: int, env: dict):
    # Kill any process using this port to prevent binding conflicts
    subprocess.run(f"kill -9 $(lsof -t -i:{port}) 2>/dev/null || true", shell=True)
    time.sleep(0.5)
    
    # Run uvicorn server in the background
    app_module = "global_state_agent:app" if name == "gsa" else f"agents.{name}.app:app"
    return subprocess.Popen(
        [str(PROJECT_ROOT / ".venv/bin/uvicorn"), app_module, "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_ROOT / "server"),
        env=env
    )

def main():
    db_dir = os.environ.get("DOCUMENTARY_LOG_DIR", "/tmp/documentary-pipeline")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "server")

    # Start GSA and all 5 agents
    servers = {
        "gsa": 8000,
        "scenario": 8001,
        "audio": 8002,
        "provisioner": 8003,
        "video": 8004,
        "assembly": 8005
    }
    
    processes = []
    try:
        for name, port in servers.items():
            print(f"Starting {name} agent on port {port}...")
            p = start_server(name, port, env)
            processes.append(p)
            
            # Wait for server health response
            delay = 0.2
            for _ in range(15):
                try:
                    resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)
                    if resp.status_code == 200:
                        break
                except Exception:
                    pass
                time.sleep(delay)
                delay = min(delay * 1.5, 2.0)

        # Loop and monitor GSA status to completion
        print("Universal Runner active. Monitoring GSA for 'done' or 'aborted' status...")
        while True:
            try:
                resp = httpx.get("http://127.0.0.1:8000/")
                if resp.status_code == 200:
                    state = resp.json()
                    phase = state.get("state", {}).get("current_phase")
                    print(f"Current Phase: {phase} | Timeline Duration: {state.get('otio', {}).get('duration_sec')}s")
                    if phase == "done":
                        print("Universal Runner detected pipeline completion!")
                        break
                    elif phase == "aborted":
                        print("Universal Runner detected pipeline abort.")
                        sys.exit(1)
            except Exception:
                pass
            time.sleep(5.0)

    except KeyboardInterrupt:
        print("Interrupted. Shutting down servers...")
    finally:
        for p in processes:
            p.terminate()
            p.wait()
        subprocess.run("kill -9 $(lsof -t -i:8000-8005) 2>/dev/null || true", shell=True)

if __name__ == "__main__":
    main()
