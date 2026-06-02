import asyncio
import sys
import os
import subprocess
import time
import httpx
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from effects import PipelineStarted, BudgetSet, UpdateScript, ScriptBlock
from event_store import EventStore
from agent_base import execute_agent_turn

def start_gsa():
    subprocess.run("kill -9 $(lsof -t -i:8000) 2>/dev/null || true", shell=True)
    time.sleep(0.5)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
    env["DOCUMENTARY_LOG_DIR"] = "/tmp/documentary-pipeline"
    
    proc = subprocess.Popen(
        [str(PROJECT_ROOT / ".venv/bin/uvicorn"), "global_state_agent:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(PROJECT_ROOT / "server"),
        env=env
    )
    
    for _ in range(15):
        try:
            resp = httpx.get("http://localhost:8000/", timeout=1.0)
            if resp.status_code in (200, 400):
                return proc
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("GSA failed to start")

async def main():
    db_dir = "/tmp/documentary-pipeline"
    import shutil
    try:
        shutil.rmtree(db_dir)
    except Exception:
        pass
    os.makedirs(db_dir, exist_ok=True)
    
    event_store = EventStore(log_dir=db_dir)
    event_store._init_db()
    
    event_store.append(PipelineStarted(agent="operator"), "")
    event_store.append(BudgetSet(agent="operator", budget_usd=10.0), "")
    
    # Create an UpdateScript block that needs processing
    block = ScriptBlock(
        scene_num=1,
        block_id="s1_b1",
        speaker="V1_Narrator",
        text="This is a test block for audio reconciliation.",
        duration_sec=5.0
    )
    event_store.append(UpdateScript(agent="scenario", blocks=[block]), "initial_hash")
    
    # Start GSA
    gsa = start_gsa()
    try:
        print("Running execute_agent_turn for audio...")
        effects = await execute_agent_turn(
            role="audio",
            gsa_url="http://localhost:8000/",
            notification_type="instruction"
        )
        print("Completed! Effects:", effects)
        
        # Read events
        all_events = event_store.read_all()
        print("\nAll Events in Event Store:")
        for r in all_events:
            print(f"- {r.seq}: {r.effect.kind} by {r.effect.agent}")
            if r.effect.kind == "queue_job":
                print(f"  Job: {r.effect}")
                
        # Read debug log
        log_path = "/tmp/documentary-pipeline/agent_debug_audio.log"
        if os.path.exists(log_path):
            print("\nAgent Debug Log:")
            with open(log_path) as f:
                print(f.read())
        else:
            print("\nNo debug log found at", log_path)

    finally:
        gsa.kill()
        gsa.wait()

if __name__ == "__main__":
    asyncio.run(main())
