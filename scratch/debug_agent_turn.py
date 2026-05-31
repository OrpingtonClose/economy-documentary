import os
import sys
import asyncio
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from agent_base import execute_agent_turn
from event_store import EventStore
from effects import PipelineStarted, BudgetSet

async def main():
    run_id = "debug_run_123"
    event_store = EventStore(log_dir="/Users/orpington/documentary-pipeline")
    # Clean previous debug runs
    import glob
    for f in glob.glob("/Users/orpington/documentary-pipeline/events_debug_run_123*"):
        try:
            os.remove(f)
        except Exception:
            pass

    event_store.append(run_id, PipelineStarted(run_id=run_id, agent="operator"), "")
    event_store.append(run_id, BudgetSet(run_id=run_id, agent="operator", budget_usd=10.0), "")

    print("Running agent turn...")
    effects = await execute_agent_turn(
        run_id=run_id,
        role="scenario",
        gsa_url="http://localhost:8000/",
        notification_type="instruction",
        context={"instruction": "write the first draft script for Lacan's objet petit a"}
    )
    print("Done! Effects:")
    print(effects)

if __name__ == "__main__":
    asyncio.run(main())
