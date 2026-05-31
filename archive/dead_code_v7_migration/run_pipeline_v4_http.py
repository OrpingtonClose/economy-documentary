"""Pipeline v4 with HTTP agents — unified launcher + orchestrator.

Usage:
    python run_pipeline_v4_http.py "a 30 second documentary about rainbows"

This script:
1. Launches each agent as an independent HTTP service (separate process)
2. Waits for all agents to be ready (polls indefinitely — operator can intervene)
3. Runs the v4 orchestrator loop, calling agents via HTTP
4. Shuts down agents when done
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from run_pipeline_v4 import run_pipeline

_AGENT_PORTS: dict[str, int] = {
    "scenario": 9001,
    "audio": 9002,
    "video": 9003,
    "assembly": 9004,
    "provisioner": 9005,
    "orchestrator": 9006,
}


def _build_agent_registry() -> dict[str, str]:
    return {
        agent_id: f"http://localhost:{port}"
        for agent_id, port in _AGENT_PORTS.items()
    }


def _wait_for_agents(registry: dict[str, str]) -> bool:
    """Poll each agent's GET / until all respond. Loops until ready."""
    import urllib.request

    ready: set[str] = set()
    while len(ready) < len(registry):
        for agent_id, url in registry.items():
            if agent_id in ready:
                continue
            try:
                req = urllib.request.Request(url, method="GET")
                # No socket timeout — operator intervenes if this hangs
                with urllib.request.urlopen(req) as resp:
                    if resp.status == 200:
                        ready.add(agent_id)
                        print(f"  [READY] {agent_id} agent at {url}")
            except Exception:
                pass
        if len(ready) < len(registry):
            print(f"  [WAIT] {len(ready)}/{len(registry)} agents ready... polling again")
            time.sleep(0.5)
    return True


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python run_pipeline_v4_http.py <brief>", file=sys.stderr)
        sys.exit(1)

    brief = " ".join(sys.argv[1:])
    output_dir = os.path.join(_SCRIPT_DIR, "pipeline_output")

    agent_registry = _build_agent_registry()
    print(f"[LAUNCHER] Agent registry: {agent_registry}")

    # Start the multi-agent launcher as a subprocess
    print("[LAUNCHER] Starting agent services...")
    server_cmd = [sys.executable, "-m", "strands_agents.launcher"]
    for agent_id, port in _AGENT_PORTS.items():
        server_cmd.extend([f"--{agent_id}-port", str(port)])

    proc = subprocess.Popen(
        server_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    print("[LAUNCHER] Waiting for agents to be ready...")
    if not _wait_for_agents(agent_registry):
        print("[LAUNCHER] FAILED to start agents. Aborting.")
        proc.terminate()
        sys.exit(1)

    # Run pipeline
    print("[LAUNCHER] Agents ready. Starting pipeline...")
    try:
        result = asyncio.run(run_pipeline(brief, output_dir, agent_registry=agent_registry))
        print(f"\nResult: {result}")
    except KeyboardInterrupt:
        print("\n[LAUNCHER] Interrupted by user.")
    finally:
        print("[LAUNCHER] Shutting down agents...")
        proc.terminate()
        time.sleep(1)
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    main()
