import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from agent_base import execute_agent_turn
from event_store import EventStore
from effects import PipelineStarted

async def main():
    run_id = "diagnostic_scenario_run"
    store = EventStore(log_dir="/tmp/documentary-pipeline")
    
    # Clean previous diagnostic run db if any
    db_path = f"/tmp/documentary-pipeline/events_{run_id}.db"
    if os.path.exists(db_path):
        os.remove(db_path)
        
    # Start the run
    start_event = PipelineStarted(
        run_id=run_id,
        agent="operator",
        config={
            "topic": "Lacan's notion of objet petit a",
            "target_duration_sec": 60.0
        }
    )
    store.append(run_id, start_event)
    
    print("Executing Scenario Agent turn...")
    try:
        # We need to boot GSA or mock GSA url. Let's start the turn.
        # GSA is not running, so we'll mock the GSA URL or just run it with http://localhost:8000/ (if it runs).
        # Wait, if GSA is not running, execute_agent_turn might fail when checking projections or hashing.
        # But let's run it anyway and observe.
        effects = await execute_agent_turn(
            run_id=run_id,
            role="scenario",
            gsa_url="http://localhost:8000/",
            context={"instruction": "Generate a short 1-minute documentary about Lacan's notion of objet petit a."}
        )
        print("Success! Effects extracted:")
        for e in effects:
            print("-", e.kind)
    except Exception as exc:
        print("Failed with error:", exc)

if __name__ == "__main__":
    asyncio.run(main())
