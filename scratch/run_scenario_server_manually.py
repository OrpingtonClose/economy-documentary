import os
import sys
import shutil
import tempfile
import asyncio
import httpx
import json
import subprocess
import time
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(PROJECT_ROOT, "server"))

def stream_logs(pipe, prefix):
    for line in iter(pipe.readline, ""):
        print(f"[{prefix}] {line.strip()}", flush=True)

async def main():
    tmpdir = tempfile.mkdtemp()
    print("Using temp dir:", tmpdir, flush=True)
    
    with open("/tmp/active_pipeline_log_dir.txt", "w", encoding="utf-8") as f:
        f.write(tmpdir)
        
    ports = {"gsa": 65301, "scenario": 65302}
    with open("/tmp/active_pipeline_ports.json", "w", encoding="utf-8") as f:
        json.dump(ports, f)
        
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(PROJECT_ROOT, "server")
    env["PYTHONUNBUFFERED"] = "1"
    
    # Start GSA
    gsa_proc = subprocess.Popen(
        [sys.executable, "global_state_agent.py", str(ports["gsa"])],
        cwd=os.path.join(PROJECT_ROOT, "server"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Start Scenario
    scenario_proc = subprocess.Popen(
        [sys.executable, "agents/scenario/app.py", str(ports["scenario"])],
        cwd=os.path.join(PROJECT_ROOT, "server"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Start log streaming threads
    threading.Thread(target=stream_logs, args=(gsa_proc.stdout, "GSA-OUT"), daemon=True).start()
    threading.Thread(target=stream_logs, args=(gsa_proc.stderr, "GSA-ERR"), daemon=True).start()
    threading.Thread(target=stream_logs, args=(scenario_proc.stdout, "SC-OUT"), daemon=True).start()
    threading.Thread(target=stream_logs, args=(scenario_proc.stderr, "SC-ERR"), daemon=True).start()
    
    # Wait for startup
    await asyncio.sleep(5.0)
    
    # Send request
    try:
        from event_store import EventStore
        from effects import PipelineStarted
        store = EventStore(log_dir=tmpdir)
        store._init_db()
        store.append(PipelineStarted(agent="operator", output_path=f"{tmpdir}/final.mp4"), "")
        
        print("Sending HTTP POST to scenario agent...", flush=True)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{ports['scenario']}/",
                content="Create a script with 2 blocks about global interest rates.",
                timeout=None
            )
            print("Response status:", resp.status_code, flush=True)
            print("Response body:", resp.text, flush=True)
    except Exception as e:
        print("HTTP request failed:", e, flush=True)
    finally:
        # Stop servers
        print("Terminating processes...", flush=True)
        gsa_proc.terminate()
        scenario_proc.terminate()
        gsa_proc.wait()
        scenario_proc.wait()
        
        shutil.rmtree(tmpdir)
        for path in ["/tmp/active_pipeline_log_dir.txt", "/tmp/active_pipeline_ports.json"]:
            if os.path.exists(path):
                os.remove(path)

if __name__ == "__main__":
    asyncio.run(main())
