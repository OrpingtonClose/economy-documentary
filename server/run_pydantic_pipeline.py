"""Entry point for pydantic-graph + pydantic-deep pipeline.

1. Destroy orphan VMs
2. Launch all agents as HTTP services
3. Run the graph orchestrator
4. Clean up on exit
"""

from __future__ import annotations

import asyncio
import os
import sys

# Ensure server/ is on sys.path
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from pydantic_graph_pipeline import AgentURLs, PipelineState, pipeline_graph
from pydantic_deep_agents.launcher import launch_all, terminate_all, wait_for_agents


async def main(brief: str, output_dir: str) -> str:
    """Run the documentary pipeline.

    Args:
        brief: The documentary brief (e.g. "A 30-second documentary about rainbows")
        output_dir: Where to write output files

    Returns:
        Pipeline result string
    """
    # 1. Destroy orphan VMs from previous runs
    print("[CLEANUP] Destroying orphan VMs...")
    from strands_agents.run_strands import _destroy_all_vms
    _destroy_all_vms()

    # 2. Launch all agents as HTTP services
    print("[LAUNCH] Starting agents...")
    processes = launch_all()

    if not wait_for_agents(processes, timeout=30):
        print("[ERROR] Agents failed to start")
        terminate_all(processes)
        return "Failed: agents did not start"

    print(f"[LAUNCH] {len(processes)} agents running")

    # 3. Build pipeline state
    timeline_dir = os.path.join(output_dir, "timelines")
    os.makedirs(timeline_dir, exist_ok=True)
    timeline_path = os.path.join(timeline_dir, "documentary_draft.otio")
    event_log_path = os.path.join(output_dir, "events.jsonl")

    state = PipelineState(
        current_task=brief,
        timeline_path=timeline_path,
        event_log_path=event_log_path,
        run_id=f"run_{int(__import__('time').time())}",
    )

    deps = AgentURLs()

    # 4. Run the graph
    print(f"[PIPELINE] Starting: {brief[:60]}")
    try:
        result = await pipeline_graph.run(state=state, deps=deps, inputs=brief)
        print(f"[PIPELINE] Complete: {result}")
        return str(result)
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
        # 5. Clean up
        print("[CLEANUP] Terminating agents...")
        terminate_all(processes)
        print("[CLEANUP] Done")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Documentary Pipeline")
    parser.add_argument("--brief", default="A 30-second documentary about rainbows")
    parser.add_argument("--output-dir", default="./pipeline_output")
    args = parser.parse_args()

    result = asyncio.run(main(args.brief, args.output_dir))
    print(f"\nResult: {result}")
