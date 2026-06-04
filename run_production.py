import os
import sys
import time
import argparse
import subprocess
import httpx
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description="Production Movie Driver")
    parser.add_argument("--prompt", required=True, help="Topic/prompt of the documentary to write")
    parser.add_argument("--budget", type=float, default=20.0, help="Production budget in USD")
    parser.add_argument("--db-dir", default="/tmp/documentary-pipeline", help="Database directory")
    args = parser.parse_args()

    # Append PYTHONPATH so we can import models and database modules
    sys.path.append(str(PROJECT_ROOT / "server"))

    # 1. Initialize Event Store
    from event_store import EventStore
    from effects import PipelineStarted, BudgetSet
    
    print(f"Initializing Event Store at {args.db_dir}...")
    import shutil
    try:
        shutil.rmtree(args.db_dir)
    except Exception:
        pass
    os.makedirs(args.db_dir, exist_ok=True)
    
    store = EventStore(log_dir=args.db_dir)
    store._init_db()
    
    # Enable WAL mode for database stability
    with store._connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        
    # Seed the initial run config and budget
    store.append(PipelineStarted(agent="operator"), "")
    store.append(BudgetSet(agent="operator", budget_usd=args.budget), "")

    # 2. Launch the Universal Runner process
    print("Launching the Universal Runner...")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "server")
    env["DOCUMENTARY_LOG_DIR"] = args.db_dir

    runner = subprocess.Popen(
        [str(PROJECT_ROOT / ".venv/bin/python"), "run_pipeline.py"],
        cwd=str(PROJECT_ROOT),
        env=env
    )

    # 3. Wait a moment for GSA and agents to start and verify health
    print("Waiting for Scenario Agent to warm up...")
    scenario_healthy = False
    delay = 0.2
    for _ in range(30):
        try:
            resp = httpx.get("http://127.0.0.1:8001/", timeout=1.0)
            if resp.status_code == 200:
                scenario_healthy = True
                break
        except Exception:
            pass
        time.sleep(delay)
        delay = min(delay * 1.5, 2.0)
        
    if not scenario_healthy:
        print("Error: Scenario Agent did not become healthy in time.")
        runner.terminate()
        sys.exit(1)


    # 4. Initiate the run by sending a POST request to the Scenario Agent
    print(f"Sending prompt to Scenario Agent via POST: {args.prompt}")
    try:
        resp = httpx.post("http://127.0.0.1:8001/", content=args.prompt, timeout=120.0)  # health probe
        if resp.status_code not in (200, 204):
            print(f"Error: Scenario Agent responded with status code {resp.status_code}")
            runner.terminate()
            sys.exit(1)
    except Exception as exc:
        print(f"Failed to communicate with Scenario Agent: {exc}")
        runner.terminate()
        sys.exit(1)

    # 5. Block until the Universal Runner finishes
    print("Pipeline initialized. Forwarding monitoring control to Universal Runner...")
    try:
        runner.wait()
    except KeyboardInterrupt:
        print("Interrupted. Stopping pipeline runner...")
        runner.terminate()
        runner.wait()

if __name__ == "__main__":
    main()
