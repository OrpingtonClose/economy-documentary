from __future__ import annotations

import os
import sys
import time
import asyncio
import json
import httpx
import logging
import glob

from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

# Setup python path to import server modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT / "server"))

from effects import (
    PipelineStarted,
    BudgetSet,
    Effect,
    UpdateScript,
    ScriptBlock,
    QueueJob,
    VMAllocated,
    JobStarted,
    JobCompleted,
    DurationAdjusted,
    ReconciliationComplete,
    MergeIntoOTIO,
    PipelineComplete,
    VMDeallocated,
    NoOp,
)
from event_store import EventStore
import agent_base
import global_state_agent
import effect_parser

# Enable logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("IntegrationTest")

LOG_DIR = "/tmp/documentary-pipeline"
event_store = EventStore(log_dir=LOG_DIR)


# ===========================================================================
# Mock Agent Responses Generator based on GSA State
# ===========================================================================

async def get_mock_completion(role: str, run_id: str) -> str:
    """Simulate detailed flowery natural language reasoning for each agent role."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"http://localhost:8000/?run_id={run_id}")
        state = resp.json()

    current_phase = state.get("state", {}).get("current_phase", "init")
    slots = state.get("otio", {}).get("slots", {})
    jobs = state.get("jobs", {}).get("jobs", {})

    logger.info(f"Mocking response for '{role}' in phase '{current_phase}'...")

    if role == "scenario":
        if not slots:
            return (
                "I see that this is a brand new run with an empty timeline. "
                "I will write the narration script for scene 1. "
                "Block ID is 'intro', speaker is 'V1', text is 'Today we look at the economy and its documentaries.', "
                "duration target is 6.5 seconds. visual notes: 'Wide shot of skyscrapers.' "
                "dopamine hook: 'A suspenseful intro hook.' pronunciation hints: none."
            )
        else:
            return "I see that all script blocks are written. There are no script modifications needed."

    if role == "audio":
        dirty_slots = [addr for addr, s in slots.items() if s["status"] == "scripted"]
        tts_jobs = [j for j in jobs.values() if j["job_type"] == "tts" and j["slot_id"] in dirty_slots]

        if dirty_slots:
            if not tts_jobs:
                addr = dirty_slots[0]
                slot = slots[addr]
                return (
                    f"I see dirty block {addr} that has no audio generated. "
                    f"I decided to queue a TTS job for it. "
                    f"Job ID is 'tts_job_intro', job_type: 'tts', block_id: '{slot['block_id']}', "
                    f"scene_num: {slot['scene_num']}, slot_id: '{addr}', "
                    f"params: {{'voice': '{slot['speaker']}', 'text': '{slot['text']}'}}."
                )
            else:
                job = tts_jobs[0]
                if job["status"] == "completed":
                    measured_sec = job["duration_sec"]
                    scripted_sec = slots[job["slot_id"]]["scripted_sec"]
                    scene_num = slots[job["slot_id"]]["scene_num"]
                    return (
                        f"Block {job['slot_id']} completed with measured duration {measured_sec} seconds. "
                        f"The target scripted duration was {scripted_sec} seconds. "
                        f"The delta is {abs(measured_sec - scripted_sec):.2f} seconds, which is within tolerance. "
                        f"I judge this block as passing and reconcile it. "
                        f"DurationAdjusted for block_id: '{job['slot_id']}', slot_id: '{job['slot_id']}', "
                        f"scene_num: {scene_num}, voice_role: 'V1', "
                        f"scripted_sec: {scripted_sec}, measured_sec: {measured_sec}."
                    )
                else:
                    return "Waiting for the TTS generation job to complete."
        else:
            if not state.get("jobs", {}).get("reconciliation_complete", False):
                return (
                    "All audio slots are clean. The reconciliation has been successfully finished. "
                    "ReconciliationComplete for block_id: 'A1:1:intro', scene_num: 1, "
                    "measured_sec: 6.8, total_measured_sec: 6.8."
                )
            return "Reconciliation complete. Waiting for subsequent phases."

    if role == "provisioner":
        pending_jobs = [j for j in jobs.values() if j["status"] == "pending"]
        if pending_jobs:
            job = pending_jobs[0]
            active_vms = state.get("vms", {}).get("vms", {})
            has_active_vm = any(v["status"] == "active" and v["role"] == job["job_type"] for v in active_vms.values())

            if not has_active_vm:
                return (
                    f"I need to provision a VM to execute pending {job['job_type']} jobs. "
                    f"I searched Vast.ai and allocated offer 1234. "
                    f"VMAllocated for instance_id 'vm_instance_1', role: '{job['job_type']}', "
                    f"offer_id: '1234', worker_url: 'http://localhost:9001', gpu_type: 'RTX 4090', "
                    f"cost_per_hour: 0.45."
                )
            else:
                vm_id = [k for k, v in active_vms.items() if v["status"] == "active" and v["role"] == job["job_type"]][0]
                return (
                    f"I see active VM {vm_id} is healthy. I will now dispatch pending job {job['job_id']} to it. "
                    f"JobStarted event for job_id '{job['job_id']}' and vm_instance_id '{vm_id}'."
                )
        else:
            running_jobs = [j for j in jobs.values() if j["status"] == "running"]
            if running_jobs:
                job = running_jobs[0]
                return (
                    f"The worker on VM reports that job {job['job_id']} has finished execution. "
                    f"Output duration: 6.8 seconds. Output saved to /tmp/out.wav. "
                    f"JobCompleted for job_id '{job['job_id']}', artifact_uri: '/tmp/out.wav', "
                    f"duration_sec: 6.8, vm_instance_id: '{job['vm_instance_id']}'."
                )

            active_vms = [k for k, v in state.get("vms", {}).get("vms", {}).items() if v["status"] == "active"]
            if active_vms:
                vm_id = active_vms[0]
                return (
                    f"There are no pending or running jobs. VM {vm_id} is currently idle. "
                    f"I will deallocate it to optimize cost. "
                    f"VMDeallocated for instance_id '{vm_id}', reason: 'job_done', "
                    f"final_cost: 0.03, runtime_sec: 240.0."
                )
            return "No infrastructure tasks to perform."

    if role == "video":
        reconciled = state.get("jobs", {}).get("reconciliation_complete", False)
        if reconciled:
            video_slots = [addr for addr, s in slots.items() if s["status"] in ("measured", "delivered")]
            ltx_jobs = [j for j in jobs.values() if j["job_type"] == "ltx"]

            if video_slots:
                addr = video_slots[0]
                slot = slots[addr]
                if not ltx_jobs:
                    return (
                        f"All audio blocks are clean. I will queue a video generation job for block {addr}. "
                        f"QueueJob for job_id: 'ltx_job_intro', job_type: 'ltx', block_id: '{slot['block_id']}', "
                        f"scene_num: {slot['scene_num']}, slot_id: '{addr}', "
                        f"params: {{'prompt': 'Skyscrapers showing financial growth.'}}."
                    )
                else:
                    job = ltx_jobs[0]
                    if job["status"] == "completed" and slot["status"] != "delivered":
                        return (
                            f"Video job {job['job_id']} completed successfully. "
                            f"I will merge the Visual mp4 track into OTIO timeline. "
                            f"MergeIntoOTIO for job_id '{job['job_id']}', block_id: '{slot['block_id']}', "
                            f"scene_num: {slot['scene_num']}, slot_id: '{addr}', "
                            f"artifact_uri: '/tmp/video.mp4', track_name: 'V1_Video', "
                            f"duration_sec: 6.8."
                        )
        return "Waiting for audio reconciliation to complete."

    if role == "assembly":
        all_delivered = len(slots) > 0 and all(s["status"] == "delivered" for s in slots.values())
        if all_delivered:
            return (
                "All audio and video slots are fully delivered. "
                "I validated the timeline and ran ffmpeg muxing. The output is healthy. "
                "PipelineComplete at output path '/tmp/final_documentary.mp4', duration_sec: 6.8."
            )
        return "Waiting for all clips to be delivered."

    return "NoOp"


# ===========================================================================
# Interceptor for bash_command tool
# ===========================================================================

def mock_bash_command(ctx, command: str) -> str:
    """Intercept all CLI / curl commands and return mock output."""
    command_lower = command.lower()

    if "gsa:8000" in command or "localhost:8000" in command:
        import subprocess
        cmd = command.replace("gsa:8000", "localhost:8000")
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return res.stdout + res.stderr

    if "9001" in command or "9002" in command or "8880" in command:
        if "tts" in command_lower:
            return "TTS generation completed for block A1:1:intro. Output saved to /tmp/audio/A1_Narration-1-intro.wav. Duration: 6.8 seconds. Model: Qwen3-TTS."
        elif "ltx" in command_lower or "video" in command_lower:
            return "Video generation completed for block A1:1:intro. Output saved to /tmp/video/intro.mp4. Duration: 6.8 seconds."

    if "vastai search offers" in command:
        return "offer_id: 1234, gpu_name: RTX 4090, vram: 24, price: 0.45\noffer_id: 5678, gpu_name: RTX A6000, vram: 48, price: 0.85"
    if "vastai create instance" in command:
        return "Started instance vm_instance_1 on offer 1234."
    if "vastai destroy instance" in command:
        return "Destroyed instance vm_instance_1."
    if "vastai show instances" in command:
        return "instance_id: vm_instance_1, status: running, ip: localhost, port: 9001"

    # Default fallback
    import subprocess
    res = subprocess.run(command, shell=True, capture_output=True, text=True)
    return res.stdout + res.stderr


# ===========================================================================
# Live Parser Interceptor / Fallback
# ===========================================================================

original_parse_agent_text = effect_parser.parse_agent_text_multi


async def mock_parse_agent_text_multi(agent_id: str, text: str, run_id: str) -> list[Effect]:
    """Try to parse using deepseek, fallback to regex mapping if offline or no key."""
    try:
        # Check if deepseek api key file is present and not empty
        api_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
        if os.path.exists(api_key_path) and os.path.getsize(api_key_path) > 0:
            logger.info("Querying live DeepSeek API for effect extraction...")
            effects = await original_parse_agent_text(agent_id, text, run_id)
            if effects and effects[0].kind != "noop":
                logger.info(f"Live DeepSeek parsed effect: {effects[0].kind}")
                return effects
    except Exception as e:
        logger.warning(f"Live DeepSeek API failed ({e}), falling back to mock parser...")

    # Mock fallback parser logic
    text_lower = text.lower()

    if "updatescript" in text_lower or "narration script for scene 1" in text_lower:
        return [UpdateScript(
            run_id=run_id,
            agent=agent_id,
            blocks=[ScriptBlock(
                scene_num=1,
                block_id="intro",
                speaker="V1",
                text="Today we look at the economy and its documentaries.",
                duration_sec=6.5
            )]
        )]

    if "queuejob" in text_lower or "queue a tts job" in text_lower:
        job_type = "ltx" if "ltx" in text_lower else "tts"
        job_id = "ltx_job_intro" if job_type == "ltx" else "tts_job_intro"
        return [QueueJob(
            run_id=run_id,
            agent=agent_id,
            job_id=job_id,
            job_type=job_type,
            scene_num=1,
            block_id="intro",
            slot_id="A1:1:intro"
        )]

    if "vmallocated" in text_lower or "provisioned offer" in text_lower:
        role = "ltx" if "ltx" in text_lower else "tts"
        return [VMAllocated(
            run_id=run_id,
            agent=agent_id,
            instance_id="vm_instance_1",
            role=role,
            offer_id="1234",
            worker_url="http://localhost:9001",
            gpu_type="RTX 4090",
            cost_per_hour=0.45
        )]

    if "jobstarted" in text_lower or "dispatch pending job" in text_lower or "start job" in text_lower:
        job_id = "ltx_job_intro" if "ltx_job_intro" in text_lower or "ltx" in text_lower else "tts_job_intro"
        return [JobStarted(
            run_id=run_id,
            agent=agent_id,
            job_id=job_id,
            vm_instance_id="vm_instance_1"
        )]

    if "jobcompleted" in text_lower or "completed successfully" in text_lower or "reports that job" in text_lower:
        job_id = "ltx_job_intro" if "ltx_job_intro" in text_lower or "ltx" in text_lower else "tts_job_intro"
        artifact = "/tmp/video.mp4" if "ltx" in job_id else "/tmp/out.wav"
        return [JobCompleted(
            run_id=run_id,
            agent=agent_id,
            job_id=job_id,
            artifact_uri=artifact,
            duration_sec=6.8,
            vm_instance_id="vm_instance_1"
        )]

    if "durationadjusted" in text_lower or "judge this block as passing" in text_lower:
        return [DurationAdjusted(
            run_id=run_id,
            agent=agent_id,
            block_id="A1:1:intro",
            slot_id="A1:1:intro",
            scene_num=1,
            voice_role="V1",
            scripted_sec=6.5,
            measured_sec=6.8
        )]

    if "reconciliationcomplete" in text_lower or "reconciliation is complete" in text_lower:
        return [ReconciliationComplete(
            run_id=run_id,
            agent=agent_id,
            blocks_total=1,
            blocks_passed=1,
            blocks_failed=0,
            worst_delta_sec=0.3,
            total_measured_sec=6.8
        )]

    if "mergeintootio" in text_lower or "merge the visual" in text_lower:
        return [MergeIntoOTIO(
            run_id=run_id,
            agent=agent_id,
            job_id="ltx_job_intro",
            block_id="intro",
            scene_num=1,
            slot_id="A1:1:intro",
            artifact_uri="/tmp/video.mp4",
            track_name="V1_Video",
            duration_sec=6.8
        )]

    if "pipelinecomplete" in text_lower or "all tracks are fully delivered" in text_lower:
        return [PipelineComplete(
            run_id=run_id,
            agent=agent_id,
            output_path="/tmp/final_documentary.mp4",
            duration_sec=6.8
        )]

    if "vmdeallocated" in text_lower or "destroying idle vm" in text_lower:
        return [VMDeallocated(
            run_id=run_id,
            agent=agent_id,
            instance_id="vm_instance_1",
            reason="job_done"
        )]

    return [NoOp(run_id=run_id, agent=agent_id, reason="No effects matched in fallback")]


# ===========================================================================
# Integration Test Runner
# ===========================================================================

async def run_server(app, port):
    """Start uvicorn server in the current event loop."""
    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    return server


async def main():
    logger.info("Initializing temporary DB directory...")
    # Clean out any old runs in tmp log dir
    if os.path.exists(LOG_DIR):
        import shutil
        for file in glob.glob(os.path.join(LOG_DIR, "events_*.db*")):
            try:
                os.remove(file)
            except Exception:
                pass
    else:
        os.makedirs(LOG_DIR)

    run_id = f"integration_run_{int(time.time())}"
    logger.info(f"Target Run ID: {run_id}")

    # Set up initial events in SQLite WAL Event Store
    start_event = PipelineStarted(
        run_id=run_id,
        agent="operator",
    )
    event_store.append(run_id, start_event, otio_hash_before="")

    budget_event = BudgetSet(
        run_id=run_id,
        agent="operator",
        budget_usd=10.0,
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

    logger.info("All servers booted. Preparing mocking layers...")

    # Define mock Agent.run method
    async def mock_run(self, prompt, deps=None, **kwargs):
        role = deps.agent_role if deps else "unknown"
        cur_run_id = run_id
        for line in prompt.split("\n"):
            if "Run ID:" in line:
                cur_run_id = line.split(":", 1)[1].strip()

        output_text = await get_mock_completion(role, cur_run_id)

        res = MagicMock()
        res.output = output_text
        res.cost = 0.01
        return res

    # Apply patches
    patcher_run = patch("pydantic_ai.Agent.run", new=mock_run)
    patcher_bash = patch("agent_base.bash_command", new=mock_bash_command)
    patcher_parse = patch("effect_parser.parse_agent_text_multi", new=mock_parse_agent_text_multi)

    patcher_run.start()
    patcher_bash.start()
    patcher_parse.start()

    logger.info("Monitors starting. Waiting for emergent pipeline convergence...")

    max_wait_seconds = 180
    converged = False
    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        try:
            # Query GSA
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://localhost:8000/?run_id={run_id}")
                if resp.status_code == 200:
                    state_data = resp.json()
                    phase = state_data.get("state", {}).get("current_phase", "init")
                    seq = state_data.get("latest_sequence", 0)
                    logger.info(f"Tick: Current Phase = '{phase}', Latest Sequence = {seq}")

                    if phase == "done":
                        logger.info("Success! Pipeline completed and validated.")
                        converged = True
                        break
                    elif phase == "aborted":
                        logger.error("Pipeline was aborted!")
                        break
        except Exception as e:
            logger.warning(f"Error querying state: {e}")

        await asyncio.sleep(2.0)

    # Clean up patches
    patcher_run.stop()
    patcher_bash.stop()
    patcher_parse.stop()

    # Shut down servers
    logger.info("Shutting down servers...")
    gsa_server.should_exit = True
    scenario_server.should_exit = True
    audio_server.should_exit = True
    video_server.should_exit = True
    assembly_server.should_exit = True
    provisioner_server.should_exit = True

    if converged:
        logger.info("Integration test PASSED successfully!")
        sys.exit(0)
    else:
        logger.error("Integration test FAILED due to timeout or abort.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
