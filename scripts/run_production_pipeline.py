from __future__ import annotations

import os
import sys
import time
import asyncio
import httpx
import logging
import glob
from pathlib import Path

# Setup python path to import server modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from effects import PipelineStarted, BudgetSet
from event_store import EventStore

# Enable logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ProductionPipeline")

LOG_DIR = "/tmp/documentary-pipeline"
event_store = EventStore(log_dir=LOG_DIR)


async def run_server(app, port):
    """Start uvicorn server in the current event loop."""
    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    return server


async def main():
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

    resume_run_id = sys.argv[1] if len(sys.argv) > 1 else None
    if resume_run_id:
        run_id = resume_run_id
        logger.info(f"Resuming production run: {run_id}")
    else:
        run_id = f"production_run_{int(time.time())}"
        logger.info(f"Starting production run: {run_id}")

        # Set up initial events in SQLite WAL Event Store
        start_event = PipelineStarted(
            run_id=run_id,
            agent="operator",
            config={
                "topic": "Lacan's notion of objet petit a",
                "target_duration_sec": 60.0
            }
        )
        event_store.append(run_id, start_event, otio_hash_before="")

        budget_event = BudgetSet(
            run_id=run_id,
            agent="operator",
            budget_usd=15.0,  # 15 USD budget
            reason="run_start",
        )
        event_store.append(run_id, budget_event, otio_hash_before="")

    logger.info("Booting GSA and Agent servers...")
    # Import apps dynamically
    from global_state_agent import app as gsa_app
    from agents.scenario import app as scenario_app
    from agents.audio import app as audio_app
    from agents.video import app as video_app
    from agents.assembly import app as assembly_app
    from provisioner.main import app as provisioner_app

    # Start all HTTP servers in background tasks in the same loop
    gsa_server = await run_server(gsa_app, 8000)
    scenario_server = await run_server(scenario_app, 8001)
    audio_server = await run_server(audio_app, 8002)
    video_server = await run_server(video_app, 8003)
    assembly_server = await run_server(assembly_app, 8005)
    provisioner_server = await run_server(provisioner_app, 8081)

    logger.info("All servers booted. Waiting 5 seconds to initialize...")
    await asyncio.sleep(5.0)

    if not resume_run_id:
        # Post initial instruction to Scenario Agent to trigger script writing (non-blocking, fast connection timeout)
        logger.info("Posting instruction to Scenario Agent...")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "http://localhost:8001/",
                    json={
                        "run_id": run_id,
                        "notification_type": "instruction",
                        "context": {
                            "instruction": "Generate a short 1-minute documentary about Lacan's notion of objet petit a (petit object a)."
                        }
                    },
                    timeout=5.0
                )
                logger.info(f"Scenario Agent trigger response: {resp.status_code} - {resp.text}")
        except Exception as exc:
            logger.error(f"Failed to post instruction to Scenario Agent: {exc}")

    logger.info("Monitors starting. Watching emergent pipeline convergence...")
    
    last_seq = 0
    last_activity_time = time.time()
    inactivity_timeout_sec = 300.0  # 5 minutes without any progress (events)

    # We will poll GSA and report pipeline status every 10 seconds
    try:
        while True:
            try:
                # 1. Query GSA state
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://localhost:8000/?run_id={run_id}")
                    if resp.status_code == 200:
                        state_data = resp.json()
                        phase = state_data.get("state", {}).get("current_phase", "init")
                        seq = state_data.get("latest_sequence", 0)
                        budget = state_data.get("budget", {})
                        logger.info(f"Monitor: Phase = '{phase}', Seq = {seq}, Budget = {budget.get('spent_usd', 0.0):.2f}/{budget.get('budget_cap_usd', 0.0):.2f}")
                        
                        if seq > last_seq:
                            last_seq = seq
                            last_activity_time = time.time()

                        if phase == "done":
                            logger.info("Success! Movie has been successfully generated and compiled!")
                            break
                        elif phase == "aborted":
                            logger.error("Pipeline run was aborted!")
                            break

                        # Material check: budget exceeded
                        if budget.get("exceeded", False):
                            logger.error("Material Timeout: Budget limit exceeded! Aborting pipeline...")
                            break
            except Exception as e:
                logger.warning(f"Error querying state from GSA: {e}")

            # 2. Check agents health check endpoints for ERROR status
            agent_ports = {
                "scenario": 8001,
                "audio": 8002,
                "video": 8003,
                "assembly": 8005,
                "provisioner": 8081,
            }
            agent_error_detected = False
            any_agent_busy = False
            for agent_name, port in agent_ports.items():
                try:
                    async with httpx.AsyncClient() as client:
                        a_resp = await client.get(f"http://localhost:{port}/?run_id={run_id}", timeout=2.0)
                        if a_resp.status_code == 200:
                            h_data = a_resp.json()
                            status = h_data.get("status")
                            if status == "error":
                                logger.error(f"Material Timeout: Agent '{agent_name}' is in ERROR state: {h_data.get('last_error')}")
                                agent_error_detected = True
                                break
                            elif status == "busy":
                                any_agent_busy = True
                except Exception as e:
                    pass
            if agent_error_detected:
                break

            # 3. Check for inactivity timeout (only when there are no running VMs or pending jobs, and no busy agents)
            has_active_work = False
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://localhost:8000/?run_id={run_id}")
                    if resp.status_code == 200:
                        state_data = resp.json()
                        jobs = state_data.get("jobs", {}).get("jobs", {})
                        has_pending_jobs = any(j.get("status") in ("pending", "running") for j in jobs.values())
                        active_vms = state_data.get("vms", {}).get("active_count", 0) > 0
                        if has_pending_jobs or active_vms:
                            has_active_work = True
            except Exception:
                pass

            if not has_active_work and not any_agent_busy and (time.time() - last_activity_time > inactivity_timeout_sec):
                logger.error(f"Material Timeout: No new events for {inactivity_timeout_sec}s, no active jobs/VMs, and no busy agents.")
                break

            await asyncio.sleep(10.0)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        gsa_server.should_exit = True
        scenario_server.should_exit = True
        audio_server.should_exit = True
        video_server.should_exit = True
        assembly_server.should_exit = True
        provisioner_server.should_exit = True
        logger.info("All servers stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
