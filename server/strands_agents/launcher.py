"""Launch all agent HTTP services as independent processes.

Usage:
    python -m strands_agents.launcher \
        --scenario-port 9001 \
        --audio-port 9002 \
        --video-port 9003 \
        --otio-port 9004 \
        --assembly-port 9005 \
        --provisioner-port 9006

Each agent runs in its own process with its own strands.Agent instance.
The graph orchestrator connects to them via AgentHTTPClient.
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

# Ensure server/ is on sys.path for imports
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SERVER_DIR = os.path.dirname(_SCRIPT_DIR)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from strands_agents.agent_http_service import build_agent_app


_DEFAULT_PORTS: dict[str, int] = {
    "scenario": 9001,
    "audio": 9002,
    "video": 9003,
    "otio": 9004,
    "assembly": 9005,
    "provisioner": 9006,
}


def _build_agent(node_id: str, model: Any | None) -> Any:
    """Build a strands.Agent for the given node."""
    from strands_agents.graph_pipeline import (
        _build_scenario_agent,
        _build_audio_agent,
        _build_video_agent,
        _build_otio_gate_agent,
        _build_assembly_agent,
        _build_provisioner_agent,
    )

    builders = {
        "scenario": _build_scenario_agent,
        "audio": _build_audio_agent,
        "video": _build_video_agent,
        "otio": _build_otio_gate_agent,
        "assembly": _build_assembly_agent,
        "provisioner": _build_provisioner_agent,
    }
    builder = builders.get(node_id)
    if builder is None:
        raise ValueError(f"Unknown node_id: {node_id}")
    return builder(model)


def _run_service(node_id: str, port: int, model: Any | None, pipeline_dir: str) -> None:
    """Worker process: build agent, inject shared state, wrap in FastAPI, serve."""
    # Inject shared OTIO manager into stage modules so tools can read/write pipeline metadata
    try:
        from strands_agents.otio_manager import OTIOStateManager
        _shared_otio_manager = OTIOStateManager(output_dir=pipeline_dir)
        timeline_dir = os.path.join(pipeline_dir, "timelines")
        draft_path = os.path.join(timeline_dir, "documentary_draft.otio")
        if os.path.exists(draft_path):
            _shared_otio_manager._timeline_path = draft_path
            _shared_otio_manager.refresh_from_disk()

        import strands_agents.stages.audio_stage as _audio_stage_mod
        import strands_agents.stages.production_stage as _production_stage_mod
        import strands_agents.stages.scenario_stage as _scenario_stage_mod
        _audio_stage_mod._otio_manager = _shared_otio_manager
        _production_stage_mod._otio_manager = _shared_otio_manager
        _scenario_stage_mod._otio_manager = _shared_otio_manager
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to inject OTIOStateManager in agent service: %s", exc)

    agent = _build_agent(node_id, model)
    app = build_agent_app(agent, name=node_id)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch agent HTTP services")
    for node_id, default_port in _DEFAULT_PORTS.items():
        parser.add_argument(
            f"--{node_id}-port",
            type=int,
            default=default_port,
            help=f"Port for {node_id} agent (default: {default_port})",
        )
    parser.add_argument("--model-id", default="", help="Model ID for agents")
    parser.add_argument("--api-key", default="", help="API key for model")
    parser.add_argument("--base-url", default="", help="Base URL for model")
    args = parser.parse_args()

    # Build model if credentials provided
    model = None
    if args.api_key:
        from strands_agents.run_strands import _get_model
        model = _get_model(args.model_id or "deepseek-chat", args.api_key, args.base_url or None)

    pipeline_dir = os.environ.get("PIPELINE_DIR", "/tmp/documentary-pipeline")
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

    for node_id in _DEFAULT_PORTS:
        port = getattr(args, f"{node_id}_port")
        p = multiprocessing.Process(
            target=_run_service,
            args=(node_id, port, model, pipeline_dir),
            name=f"agent-{node_id}",
        )
        p.start()
        processes.append(p)
        print(f"[launcher] {node_id} agent on port {port} (pid {p.pid})")

    print("[launcher] All agents running. Press Ctrl+C to stop.")

    # Wait for all processes
    for p in processes:
        p.join()


if __name__ == "__main__":
    main()
