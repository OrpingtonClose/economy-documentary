"""Pipeline v2: Agents self-orchestrate. Instructor parses effects.

Architecture:
  Agent → free text → Instructor → Effect → Event Store
                                        ↓
                                 Projection Handler → OTIO
                                        ↓
                                     Job Queue
"""

from __future__ import annotations

import asyncio
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import httpx

from pipeline_instructor import Instructor
from pydantic_deep_agents.launcher import launch_all, terminate_all, wait_for_agents
from job_queue import clear_all_jobs


AGENT_URLS = {
    "scenario": "http://localhost:9001",
    "audio": "http://localhost:9002",
    "video": "http://localhost:9003",
    "assembly": "http://localhost:9005",
    "provisioner": "http://localhost:9006",
}


async def _call_agent(url: str, text: str) -> str:
    """Call an agent via HTTP POST with plain text."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url.rstrip("/") + "/",
            content=text,
            headers={"Content-Type": "text/plain"},
        )
        resp.raise_for_status()
        return resp.text


async def run_pipeline(
    brief: str,
    output_dir: str,
    max_cycles: int = 50,
) -> str:
    """Run the full pipeline."""
    print("[CLEANUP] Destroying orphan VMs, clearing queue...")
    from strands_agents.run_strands import _destroy_all_vms
    _destroy_all_vms()
    clear_all_jobs()

    timeline_dir = os.path.join(output_dir, "timelines")
    os.makedirs(timeline_dir, exist_ok=True)
    timeline_path = os.path.join(timeline_dir, "documentary_draft.otio")
    event_log_path = os.path.join(output_dir, "events.jsonl")

    if not os.path.exists(timeline_path):
        import opentimelineio as otio
        timeline = otio.schema.Timeline(name="documentary")
        stack = otio.schema.Stack(name="tracks")
        timeline.tracks = stack
        stack.append(otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio))
        stack.append(otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video))
        timeline.to_json_file(timeline_path)
        print(f"[INIT] Created fresh OTIO timeline: {timeline_path}")

    print("[LAUNCH] Starting agents...")
    processes = launch_all()
    if not await wait_for_agents(processes):
        print("[ERROR] Agents failed to start")
        terminate_all(processes)
        return "Failed: agents did not start"
    print(f"[LAUNCH] {len(processes)} agents running")

    print("[RESET] Clearing agent memories...")
    async with httpx.AsyncClient() as client:
        for url in AGENT_URLS.values():
            try:
                await client.post(url.rstrip("/") + "/reset")
            except Exception:
                pass
    print("[RESET] Done")

    instructors = {
        uid: Instructor(uid, event_log_path, timeline_path)
        for uid in AGENT_URLS.keys()
    }

    print(f"[PIPELINE] Starting: {brief[:60]}")

    try:
        for cycle in range(max_cycles):
            print(f"\n[CYCLE {cycle + 1}]")

            from job_queue import get_queue_summary
            audio_summary = get_queue_summary("audio")
            video_summary = get_queue_summary("video")
            pending = (
                audio_summary.get("pending", 0)
                + audio_summary.get("assigned", 0)
                + video_summary.get("pending", 0)
                + video_summary.get("assigned", 0)
            )
            has_audio = (
                audio_summary.get("completed", 0) > 0
                and audio_summary.get("pending", 0) == 0
                and audio_summary.get("assigned", 0) == 0
                and audio_summary.get("running", 0) == 0
                and audio_summary.get("needs_retry", 0) == 0
            )
            has_video = (
                video_summary.get("completed", 0) > 0
                and video_summary.get("pending", 0) == 0
                and video_summary.get("assigned", 0) == 0
                and video_summary.get("running", 0) == 0
                and video_summary.get("needs_retry", 0) == 0
            )
            total_audio = sum(audio_summary.values())
            total_video = sum(video_summary.values())
            # Audio/video are "done" when all jobs are completed (or no jobs were ever created)
            audio_done = total_audio > 0 and has_audio
            video_done = total_video > 0 and has_video

            import glob
            output_mp4s = glob.glob(os.path.join(output_dir, "output", "*.mp4"))
            has_output = len(output_mp4s) > 0

            if audio_done and video_done and has_output:
                print("[PIPELINE] Complete!")
                return f"Pipeline complete in {cycle + 1} cycles."

            units_to_poll = []
            if cycle == 0:
                units_to_poll.append("scenario")
            if not audio_done:
                units_to_poll.append("audio")
            if not video_done:
                units_to_poll.append("video")
            if pending > 0:
                units_to_poll.append("provisioner")
            if audio_done and video_done:
                units_to_poll.append("assembly")

            acted = False
            for unit in units_to_poll:
                print(f"  Polling: {unit}")
                context = f"Audio queue: {audio_summary}\nVideo queue: {video_summary}\n"
                if unit == "scenario" and cycle == 0:
                    context = f"{context}\nBrief: {brief}"

                try:
                    agent_output = await _call_agent(AGENT_URLS[unit], context)
                    print(f"  Raw: {agent_output[:200]}...")

                    effects, feedback = instructors[unit].process_multi(agent_output)
                    print(f"  Effects: {[e.effect_type for e in effects]}")

                    if effects and not all(e.effect_type == "NoOp" for e in effects):
                        acted = True

                except Exception as exc:
                    print(f"  Error: {exc}")

            if not acted and cycle > 3:
                print("[PIPELINE] No progress. Stopping.")
                return f"Pipeline stopped after {cycle + 1} cycles."

        return f"Pipeline reached max cycles ({max_cycles})."

    except Exception as exc:
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
