"""Pipeline v2: Per-unit state machines + Instructor as bridge.

Each unit (agent) has its own state machine.
The instructor parses agent text into effects, validates, stores, projects.
Feedback is sent back to the agent on next invocation.

All agent communication is via HTTP.
"""

from __future__ import annotations

import asyncio
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import httpx

from instructor import Instructor
from pydantic_deep_agents.launcher import launch_all, terminate_all, wait_for_agents


AGENT_URLS = {
    "scenario": "http://localhost:9001",
    "audio": "http://localhost:9002",
    "video": "http://localhost:9003",
    "assembly": "http://localhost:9005",
    "provisioner": "http://localhost:9006",
}


async def _call_agent(url: str, text: str, timeout: float = 300.0) -> str:
    """Call an agent via HTTP POST with plain text."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url.rstrip("/") + "/",
            content=text,
            headers={"Content-Type": "text/plain"},
        )
        resp.raise_for_status()
        return resp.text


async def run_unit(
    unit_id: str,
    task: str,
    instructor: Instructor,
    max_turns: int = 10,
) -> str:
    """Run a single unit for up to max_turns.

    Each turn:
    1. Send task + feedback to agent
    2. Agent thinks and outputs text
    3. Instructor parses, validates, stores, projects
    4. Feedback is generated
    5. If effect is NoOp, stop
    """
    url = AGENT_URLS[unit_id]
    context = task

    for turn in range(max_turns):
        # Call agent
        agent_output = await _call_agent(url, context)

        # Instructor processes
        effect, feedback = instructor.process(agent_output)

        # Check if done
        if effect.effect_type == "NoOp":
            return f"{unit_id} done after {turn + 1} turns.\n{feedback.to_text()}"

        # Build context for next turn
        context = (
            f"Your previous output:\n{agent_output}\n\n"
            f"{feedback.to_text()}\n\n"
            f"Continue working."
        )

    return f"{unit_id} reached max turns ({max_turns}).\nLast feedback:\n{feedback.to_text()}"


def _check_has_audio(timeline_path: str) -> bool:
    """Check if all audio jobs are completed and passed QA."""
    from job_queue import get_queue_summary
    summary = get_queue_summary("audio")
    # Audio is "done" when there are completed jobs and no pending/assigned/running/needs_retry
    total = sum(summary.get(s, 0) for s in ["pending", "assigned", "running", "completed", "needs_retry", "failed"])
    if total == 0:
        return False  # No jobs created yet
    return summary.get("pending", 0) + summary.get("assigned", 0) + summary.get("running", 0) + summary.get("needs_retry", 0) == 0


def _check_has_video(timeline_path: str) -> bool:
    """Check if all video jobs are completed and passed QA."""
    from job_queue import get_queue_summary
    summary = get_queue_summary("video")
    total = sum(summary.get(s, 0) for s in ["pending", "assigned", "running", "completed", "needs_retry", "failed"])
    if total == 0:
        return False  # No jobs created yet
    return summary.get("pending", 0) + summary.get("assigned", 0) + summary.get("running", 0) + summary.get("needs_retry", 0) == 0


def _check_has_output(timeline_path: str) -> bool:
    """Check if output MP4s exist."""
    import glob
    output_dir = os.path.join(os.path.dirname(timeline_path), "output")
    return len(glob.glob(os.path.join(output_dir, "*.mp4"))) > 0


def _get_pending_jobs() -> int:
    """Get count of pending/assigned jobs from queue."""
    from job_queue import get_queue_summary
    audio_summary = get_queue_summary("audio")
    video_summary = get_queue_summary("video")
    return (
        audio_summary.get("pending", 0)
        + audio_summary.get("assigned", 0)
        + video_summary.get("pending", 0)
        + video_summary.get("assigned", 0)
    )


async def run_pipeline(
    brief: str,
    output_dir: str,
    max_cycles: int = 50,
) -> str:
    """Run the full pipeline.

    Cycles through units based on world state.
    Each unit runs until it produces a NoOp.
    """
    # 1. Destroy orphan VMs
    print("[CLEANUP] Destroying orphan VMs...")
    from strands_agents.run_strands import _destroy_all_vms
    _destroy_all_vms()

    # 2. Launch agents
    print("[LAUNCH] Starting agents...")
    processes = launch_all()
    if not wait_for_agents(processes, timeout=30):
        print("[ERROR] Agents failed to start")
        terminate_all(processes)
        return "Failed: agents did not start"
    print(f"[LAUNCH] {len(processes)} agents running")

    # 3. Setup paths
    timeline_dir = os.path.join(output_dir, "timelines")
    os.makedirs(timeline_dir, exist_ok=True)
    timeline_path = os.path.join(timeline_dir, "documentary_draft.otio")
    event_log_path = os.path.join(output_dir, "events.jsonl")

    # 3b. Initialize OTIO timeline if it doesn't exist
    if not os.path.exists(timeline_path):
        import opentimelineio as otio
        timeline = otio.schema.Timeline(name="documentary")
        stack = otio.schema.Stack(name="tracks")
        timeline.tracks = stack
        stack.append(otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio))
        stack.append(otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video))
        timeline.to_json_file(timeline_path)
        print(f"[INIT] Created fresh OTIO timeline: {timeline_path}")

    # 4. Create instructors for each unit
    instructors = {
        uid: Instructor(uid, event_log_path, timeline_path)
        for uid in AGENT_URLS.keys()
    }

    # 5. Pipeline cycles
    print(f"[PIPELINE] Starting: {brief[:60]}")

    try:
        for cycle in range(max_cycles):
            print(f"\n[CYCLE {cycle + 1}]")

            # Check world state
            has_audio = _check_has_audio(timeline_path)
            has_video = _check_has_video(timeline_path)
            has_output = _check_has_output(timeline_path)
            pending = _get_pending_jobs()

            # Decide which unit to run
            if cycle == 0:
                unit = "scenario"
                task = brief
            elif not has_audio:
                unit = "audio"
                task = "Generate narration audio for all scenes."
            elif not has_video:
                unit = "video"
                task = "Generate video clips for all scenes."
            elif pending > 0:
                unit = "provisioner"
                from job_queue import get_queue_summary
                audio_summary = get_queue_summary("audio")
                video_summary = get_queue_summary("video")
                task = f"Execute pending jobs. Audio: {audio_summary}, Video: {video_summary}"
            elif not has_output:
                unit = "assembly"
                task = "Assemble final documentary from audio and video clips."
            else:
                print("[PIPELINE] Complete!")
                return f"Pipeline complete in {cycle + 1} cycles."

            print(f"  Running: {unit}")
            result = await run_unit(unit, task, instructors[unit])
            print(f"  Result: {result[:200]}...")

        return f"Pipeline reached max cycles ({max_cycles})."

    except Exception as exc:
        from maintainer import notify_maintainer
        notify_maintainer(
            operation="pipeline",
            error=str(exc),
            context={"brief": brief, "output_dir": output_dir},
        )
        print(f"[PIPELINE] Failed: {exc}")
        return f"Failed: {exc}"

    finally:
        print("[CLEANUP] Terminating agents...")
        terminate_all(processes)
        print("[CLEANUP] Done")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Documentary Pipeline v2")
    parser.add_argument("--brief", default="A 30-second documentary about rainbows")
    parser.add_argument("--output-dir", default="./pipeline_output")
    args = parser.parse_args()

    result = asyncio.run(run_pipeline(args.brief, args.output_dir))
    print(f"\nResult: {result}")
