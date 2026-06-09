import os
import sys
import shutil
import tempfile
import asyncio
import httpx
import json

# Setup paths
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(PROJECT_ROOT, "server"))

from agent_base import execute_agent_turn, get_active_log_dir
from event_store import EventStore
from effects import PipelineStarted
from config_schema import PipelineConfig

async def main():
    # Setup temporary directory
    tmpdir = tempfile.mkdtemp()
    print("Using temp dir:", tmpdir)
    
    with open("/tmp/active_pipeline_log_dir.txt", "w", encoding="utf-8") as f:
        f.write(tmpdir)
        
    ports = {"gsa": 65001, "scenario": 65002}
    with open("/tmp/active_pipeline_ports.json", "w", encoding="utf-8") as f:
        json.dump(ports, f)
        
    try:
        # Initialize EventStore
        store = EventStore(log_dir=tmpdir)
        store._init_db()
        store.append(PipelineStarted(agent="operator", output_path=f"{tmpdir}/final.mp4"), "")
        
        # Start a mock GSA health responder to satisfy the GSA check
        from fastapi import FastAPI
        import uvicorn
        import threading
        
        gsa_app = FastAPI()
        @gsa_app.get("/")
        async def gsa_health():
            return {"state": {"recent_effects": {}, "config": {}}}
            
        def run_gsa():
            uvicorn.run(gsa_app, host="127.0.0.1", port=ports["gsa"], log_level="error")
            
        t = threading.Thread(target=run_gsa, daemon=True)
        t.start()
        await asyncio.sleep(1.0) # wait for GSA to start
        
        print("Executing scenario agent turn directly...")
        
        # We run the agent turn directly!
        res = await execute_agent_turn(
            role="scenario",
            gsa_url=f"http://127.0.0.1:{ports['gsa']}/",
            notification_type="human",
            context={"instruction": "Create a script with 2 blocks about global interest rates."},
            config=PipelineConfig(),
            extra_capabilities=[]
        )
        print("Agent turn returned successfully!")
        print("Resulting effects:", res)
        
    except Exception as e:
        import traceback
        print("An error occurred during execute_agent_turn:")
        traceback.print_exc()
    finally:
        shutil.rmtree(tmpdir)
        for path in ["/tmp/active_pipeline_log_dir.txt", "/tmp/active_pipeline_ports.json"]:
            if os.path.exists(path):
                os.remove(path)

if __name__ == "__main__":
    asyncio.run(main())
