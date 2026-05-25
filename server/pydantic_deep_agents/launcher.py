"""Launch all pydantic-deep agents as independent HTTP processes.

Each agent runs in its own process with its own FastAPI app.
All communication is via HTTP. No shared state.
"""

from __future__ import annotations

import multiprocessing
from typing import Any

import uvicorn

AGENTS: dict[str, tuple[str, int]] = {
    "scenario": ("pydantic_deep_agents.scenario_agent", 9001),
    "audio": ("pydantic_deep_agents.audio_agent", 9002),
    "video": ("pydantic_deep_agents.video_agent", 9003),
    "otio_gate": ("pydantic_deep_agents.otio_gate_agent", 9004),
    "assembly": ("pydantic_deep_agents.assembly_agent", 9005),
    "provisioner": ("pydantic_deep_agents.provisioner_agent", 9006),
}


def _run_agent(module_name: str, port: int) -> None:
    """Import agent module and run uvicorn."""
    import importlib

    mod = importlib.import_module(module_name)
    app = mod.app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def launch_all() -> list[multiprocessing.Process]:
    """Launch all agents as independent processes.

    Returns:
        List of running processes. Caller is responsible for cleanup.
    """
    processes: list[multiprocessing.Process] = []
    for name, (module, port) in AGENTS.items():
        p = multiprocessing.Process(
            target=_run_agent,
            args=(module, port),
            name=f"agent-{name}",
        )
        p.start()
        processes.append(p)
    return processes


def wait_for_agents(processes: list[multiprocessing.Process], timeout: float = 30.0) -> bool:
    """Wait for all agent processes to be ready.

    Returns True if all processes are alive after timeout.
    """
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(p.is_alive() for p in processes):
            return True
        time.sleep(0.5)
    return False


def terminate_all(processes: list[multiprocessing.Process]) -> None:
    """Terminate all agent processes."""
    for p in processes:
        if p.is_alive():
            p.terminate()
    for p in processes:
        p.join(timeout=5)
        if p.is_alive():
            p.kill()
            p.join(timeout=2)
