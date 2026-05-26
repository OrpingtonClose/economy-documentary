"""Launch all agent HTTP services as independent processes.

Usage:
    python -m strands_agents.launcher \
        --scenario-port 9001 \
        --audio-port 9002 \
        --video-port 9003 \
        --assembly-port 9004 \
        --provisioner-port 9005 \
        --orchestrator-port 9006

Each agent runs in its own process with its own LLM client.
The pipeline orchestrator connects to them via AgentHTTPClient.
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import signal
import sys
import time
from typing import Any

import uvicorn

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SERVER_DIR = os.path.dirname(_SCRIPT_DIR)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

_DEFAULT_PORTS: dict[str, int] = {
    "scenario": 9001,
    "audio": 9002,
    "video": 9003,
    "assembly": 9004,
    "provisioner": 9005,
    "orchestrator": 9006,
}


def _run_service(
    node_id: str,
    port: int,
    model: str,
    pipeline_dir: str,
    agent_registry: dict[str, str],
) -> None:
    """Worker process: build agent service and serve."""
    from openai import OpenAI
    from strands_agents.agent_http_service import build_agent_http_service

    api_key = ""
    key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    if os.path.exists(key_path):
        with open(key_path) as f:
            api_key = f.read().strip()

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")

    from run_pipeline_v4 import AGENT_PROMPTS, _inject_skill_fragment
    system_prompt = _inject_skill_fragment(node_id, AGENT_PROMPTS.get(node_id, ""))

    app = build_agent_http_service(
        agent_id=node_id,
        system_prompt=system_prompt,
        client=client,
        model=model,
    )
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch agent HTTP services")
    for node_id, default_port in _DEFAULT_PORTS.items():
        parser.add_argument(
            f"--{node_id}-port",
            type=int,
            default=default_port,
            help=f"Port for {node_id} agent (default: {default_port})",
        )
    args = parser.parse_args()

    pipeline_dir = os.path.join(os.getcwd(), "pipeline_output")
    model = "deepseek-v4-flash"
    processes: list[multiprocessing.Process] = []

    def _signal_handler(signum: int, frame: Any) -> None:
        print("\n[launcher] Shutting down agents...")
        for p in processes:
            p.terminate()
        time.sleep(1)
        for p in processes:
            if p.is_alive():
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    agent_registry = {
        nid: f"http://localhost:{getattr(args, f'{nid}_port')}"
        for nid in _DEFAULT_PORTS
    }

    for node_id in _DEFAULT_PORTS:
        port = getattr(args, f"{node_id}_port")
        p = multiprocessing.Process(
            target=_run_service,
            args=(node_id, port, model, pipeline_dir, agent_registry),
            name=f"agent-{node_id}",
        )
        p.start()
        processes.append(p)
        print(f"[launcher] {node_id} agent on port {port} (pid {p.pid})")

    print("[launcher] All agents running. Press Ctrl+C to stop.")

    for p in processes:
        p.join()


if __name__ == "__main__":
    main()
