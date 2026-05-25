"""Unit test harness with VM-like trace harvesting.

Runs a single agent (e.g., audio) as an isolated subprocess VM,
intercepts all I/O, records queue state changes, and produces
a detailed execution trace.

NO MOCKS — everything is real.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
import opentimelineio as otio

from job_queue import clear_all_jobs, get_queue_summary


@dataclass
class TraceEvent:
    """A single event in the execution trace."""
    timestamp: str
    event_type: str
    detail: dict


@dataclass
class ExecutionTrace:
    """Complete trace of a single agent run."""
    agent_id: str
    started_at: str
    completed_at: str | None = None
    events: list[TraceEvent] = field(default_factory=list)

    def add(self, event_type: str, **kwargs):
        self.events.append(TraceEvent(
            timestamp=datetime.utcnow().isoformat(),
            event_type=event_type,
            detail=kwargs,
        ))

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "events": [
                {
                    "timestamp": e.timestamp,
                    "event_type": e.event_type,
                    "detail": e.detail,
                }
                for e in self.events
            ],
        }

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)


class QueueMonitor:
    """Monitors the job queue and records state changes."""

    def __init__(self, stage: str, trace: ExecutionTrace):
        self.stage = stage
        self.trace = trace
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_summary: dict | None = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop.is_set():
            summary = get_queue_summary(self.stage)
            if summary != self._last_summary:
                self.trace.add(
                    "queue_state_change",
                    stage=self.stage,
                    summary=summary,
                )
                self._last_summary = summary.copy()
            time.sleep(0.5)


async def run_agent_vm(
    agent_module: str,
    port: int,
    task_text: str,
    timeline_path: str,
    trace_dir: str,
) -> ExecutionTrace:
    """Run a single agent as a subprocess VM with full tracing.

    Args:
        agent_module: e.g. "pydantic_deep_agents.audio_agent"
        port: HTTP port for the agent
        task_text: The task to send to the agent
        timeline_path: Path to the OTIO timeline
        trace_dir: Where to save trace files

    Returns:
        ExecutionTrace with all recorded events
    """
    trace = ExecutionTrace(
        agent_id=agent_module.split(".")[-1].replace("_agent", ""),
        started_at=datetime.utcnow().isoformat(),
    )

    os.makedirs(trace_dir, exist_ok=True)
    trace.add("harness_start", agent_module=agent_module, port=port)

    # 1. Clear queue
    clear_all_jobs()
    trace.add("queue_cleared")

    # 2. Start queue monitor
    monitor = QueueMonitor("audio", trace)
    monitor.start()

    # 3. Start agent as subprocess (the "VM")
    agent_script = f"""
import sys
sys.path.insert(0, '{os.path.dirname(os.path.abspath(__file__))}')
import uvicorn
from {agent_module} import app
uvicorn.run(app, host='127.0.0.1', port={port}, log_level='warning')
"""
    trace.add("vm_start", port=port, module=agent_module)

    proc = subprocess.Popen(
        [sys.executable, "-c", agent_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # 4. Wait for agent to be ready (async TCP check)
    ready = False
    for _ in range(60):
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port),
                timeout=1.0,
            )
            writer.close()
            await writer.wait_closed()
            ready = True
            break
        except Exception:
            await asyncio.sleep(0.5)

    if not ready:
        proc.terminate()
        trace.add("vm_start_failed", reason="port never ready")
        monitor.stop()
        return trace

    trace.add("vm_ready", port=port)

    # 5. Send task to agent
    trace.add("task_send", text_preview=task_text[:200])
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/",
                content=task_text,
                headers={"Content-Type": "text/plain"},
            )
            resp.raise_for_status()
            trace.add(
                "task_response",
                status_code=resp.status_code,
                response_length=len(resp.text),
                response_preview=resp.text[:500],
            )
    except Exception as exc:
        trace.add("task_error", error=str(exc))

    # 6. Stop queue monitor and capture final state
    monitor.stop()
    final_summary = get_queue_summary("audio")
    trace.add("queue_final_state", summary=final_summary)

    # 7. Stop the VM
    proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=10)
        trace.add("vm_shutdown", stdout_len=len(stdout), stderr_len=len(stderr))
        if stderr:
            trace.add("vm_stderr", stderr=stderr[:2000])
    except subprocess.TimeoutExpired:
        proc.kill()
        trace.add("vm_shutdown_forced")

    trace.completed_at = datetime.utcnow().isoformat()

    # 8. Save trace
    trace_path = os.path.join(trace_dir, f"trace_{trace.agent_id}_{int(time.time())}.json")
    trace.save(trace_path)
    print(f"[HARNESS] Trace saved: {trace_path}")

    return trace


async def main():
    """Run audio agent VM test with full tracing."""
    print("=" * 60)
    print("UNIT VM TEST: Audio Agent with Trace Harvesting")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        timeline_path = os.path.join(tmpdir, "documentary.otio")
        trace_dir = os.path.join(tmpdir, "traces")

        # Create timeline
        timeline = otio.schema.Timeline(name="documentary")
        stack = otio.schema.Stack(name="tracks")
        timeline.tracks = stack
        stack.append(otio.schema.Track(name="A1_Narration", kind=otio.schema.TrackKind.Audio))
        stack.append(otio.schema.Track(name="V1_Video", kind=otio.schema.TrackKind.Video))
        timeline.metadata.setdefault("documentary", {})
        timeline.metadata["documentary"]["narration_v1"] = (
            "Every rainbow you've ever seen is a lie. "
            "But that doesn't make it any less real."
        )
        timeline.metadata["documentary"]["narration_v2"] = (
            "A rainbow forms when sunlight refracts through water droplets."
        )
        timeline.metadata["documentary"]["narration_v3"] = (
            "Ancient cultures called it a bridge between worlds."
        )
        timeline.to_json_file(timeline_path)

        task = (
            f"Generate narration audio for all scenes.\n\n"
            f"The script is in the OTIO timeline at:\n"
            f"{timeline_path}\n\n"
            f"Read it with bash/python, then create jobs in the audio queue "
            f"for each voice (V1, V2, V3).\n\n"
            f"Report what you created in this format:\n"
            f"Scene 1:\n"
            f"Generate narration audio for V1: [exact text]\n"
            f"Generate narration audio for V2: [exact text]\n"
            f"Generate narration audio for V3: [exact text]\n"
        )

        trace = await run_agent_vm(
            agent_module="pydantic_deep_agents.audio_agent",
            port=19002,  # Use non-standard port to avoid conflicts
            task_text=task,
            timeline_path=timeline_path,
            trace_dir=trace_dir,
        )

        # Print summary
        print("\n--- TRACE SUMMARY ---")
        print(f"Agent: {trace.agent_id}")
        print(f"Duration: {trace.started_at} → {trace.completed_at}")
        print(f"Events: {len(trace.events)}")

        job_events = [e for e in trace.events if e.event_type == "queue_state_change"]
        if job_events:
            print(f"\nQueue state changes: {len(job_events)}")
            for e in job_events:
                print(f"  {e.timestamp}: {e.detail['summary']}")

        final = get_queue_summary("audio")
        total = sum(final.values())
        print(f"\nFinal queue state: {final}")
        if total >= 3:
            print(f"[PASS] {total} job(s) created")
        elif total > 0:
            print(f"[PARTIAL] {total} job(s) created")
        else:
            print("[FAIL] No jobs created")

        clear_all_jobs()


if __name__ == "__main__":
    asyncio.run(main())
