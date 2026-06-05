import os
import sys
import time
import asyncio
import httpx
import uvicorn
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# Make sure we can import from server
sys.path.append(str(PROJECT_ROOT / "server"))

from global_state_agent import app as gsa_app
from agents.scenario.app import app as scenario_app
from agents.audio.app import app as audio_app
from agents.provisioner.app import app as provisioner_app
from agents.video.app import app as video_app
from agents.assembly.app import app as assembly_app

async def main_async():
    # Kill any processes using the agent ports in one batch to prevent conflicts
    subprocess.run("kill -9 $(lsof -t -i:8000-8005) 2>/dev/null || true", shell=True)
    await asyncio.sleep(0.1)

    # Set log directory in env if not already present, so imported apps read it
    db_dir = os.environ.get("DOCUMENTARY_LOG_DIR", "/tmp/documentary-pipeline")
    os.environ["DOCUMENTARY_LOG_DIR"] = db_dir

    configs = [
        uvicorn.Config(gsa_app, host="127.0.0.1", port=8000, log_level="warning"),
        uvicorn.Config(scenario_app, host="127.0.0.1", port=8001, log_level="warning"),
        uvicorn.Config(audio_app, host="127.0.0.1", port=8002, log_level="warning"),
        uvicorn.Config(provisioner_app, host="127.0.0.1", port=8003, log_level="warning"),
        uvicorn.Config(video_app, host="127.0.0.1", port=8004, log_level="warning"),
        uvicorn.Config(assembly_app, host="127.0.0.1", port=8005, log_level="warning"),
    ]
    
    for c in configs:
        c.setup_event_loop = False
    servers = [uvicorn.Server(c) for c in configs]
    
    # Start all servers concurrently in the background
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    
    # Wait for all servers to become healthy
    print("Waiting for servers to become healthy...")
    for port in range(8000, 8006):
        healthy = False
        for _ in range(50):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://127.0.0.1:{port}/", timeout=1.0)
                    if resp.status_code == 200:
                        healthy = True
                        break
            except Exception:
                pass
            await asyncio.sleep(0.05)
        if not healthy:
            print(f"Error: Server on port {port} did not start up.", file=sys.stderr)
            for s in servers:
                s.should_exit = True
            await asyncio.gather(*tasks, return_exceptions=True)
            sys.exit(1)

    print("All servers running. Universal Runner active. Monitoring GSA for 'done' or 'aborted' status...")
    
    # Loop and monitor GSA status to completion
    try:
        while True:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get("http://127.0.0.1:8000/", timeout=1.0)
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
            await asyncio.sleep(0.25)
    finally:
        print("Shutting down servers...")
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        subprocess.run("kill -9 $(lsof -t -i:8000-8005) 2>/dev/null || true", shell=True)

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("Interrupted. Exiting...")
        subprocess.run("kill -9 $(lsof -t -i:8000-8005) 2>/dev/null || true", shell=True)

if __name__ == "__main__":
    main()
