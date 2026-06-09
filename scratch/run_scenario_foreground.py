import os
import sys
import shutil
import tempfile
import json
import subprocess
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def main():
    tmpdir = tempfile.mkdtemp()
    print("Using temp dir:", tmpdir)
    
    with open("/tmp/active_pipeline_log_dir.txt", "w", encoding="utf-8") as f:
        f.write(tmpdir)
        
    ports = {"gsa": 65201, "scenario": 65202}
    with open("/tmp/active_pipeline_ports.json", "w", encoding="utf-8") as f:
        json.dump(ports, f)
        
    # Initialize EventStore
    sys.path.append(os.path.join(PROJECT_ROOT, "server"))
    from event_store import EventStore
    from effects import PipelineStarted
    store = EventStore(log_dir=tmpdir)
    store._init_db()
    store.append(PipelineStarted(agent="operator", output_path=f"{tmpdir}/final.mp4"), "")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(PROJECT_ROOT, "server")
    env["PYTHONUNBUFFERED"] = "1"

    # Start GSA
    gsa_proc = subprocess.Popen(
        [sys.executable, "global_state_agent.py", str(ports["gsa"])],
        cwd=os.path.join(PROJECT_ROOT, "server"),
        env=env
    )
    
    # Start Scenario
    scenario_proc = subprocess.Popen(
        [sys.executable, "agents/scenario/app.py", str(ports["scenario"])],
        cwd=os.path.join(PROJECT_ROOT, "server"),
        env=env
    )
    
    print("Processes spawned. GSA PID:", gsa_proc.pid, "Scenario PID:", scenario_proc.pid)
    
    time.sleep(2.0)
    
    print("Sending HTTP request...")
    try:
        import httpx
        resp = httpx.post(
            f"http://127.0.0.1:{ports['scenario']}/",
            content="Create a script with 2 blocks about global interest rates.",
            timeout=120.0
        )
        print("Response status:", resp.status_code)
        print("Response text:", resp.text)
    except Exception as e:
        print("HTTP request failed:", e)
    finally:
        gsa_proc.terminate()
        scenario_proc.terminate()
        shutil.rmtree(tmpdir)
        for path in ["/tmp/active_pipeline_log_dir.txt", "/tmp/active_pipeline_ports.json"]:
            if os.path.exists(path):
                os.remove(path)

if __name__ == "__main__":
    main()
