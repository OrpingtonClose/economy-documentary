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
    "assembly": ("pydantic_deep_agents.assembly_agent", 9005),
    "provisioner": ("pydantic_deep_agents.provisioner_agent", 9006),
}


def _run_agent(module_name: str, port: int, env_vars: dict[str, str] | None = None) -> None:
    """Import agent module and run uvicorn."""
    import importlib
    import os
    import sys

    # Set env vars before any imports that depend on them
    if env_vars:
        for key, value in env_vars.items():
            os.environ[key] = value

    # Ensure server/ is on Python path so agents can import job_queue, models, etc.
    server_dir = os.path.join(os.path.dirname(__file__), "..")
    server_dir = os.path.abspath(server_dir)
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)

    mod = importlib.import_module(module_name)
    app = mod.app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def launch_all(env_vars: dict[str, str] | None = None) -> list[multiprocessing.Process]:
    """Launch all agents as independent processes.

    Args:
        env_vars: Environment variables to set in each agent process.

    Returns:
        List of running processes. Caller is responsible for cleanup.
    """
    processes: list[multiprocessing.Process] = []
    for name, (module, port) in AGENTS.items():
        p = multiprocessing.Process(
            target=_run_agent,
            args=(module, port, env_vars),
            name=f"agent-{name}",
        )
        p.start()
        processes.append(p)
    return processes


async def wait_for_agents(processes: list[multiprocessing.Process]) -> bool:
    """Wait for all agent HTTP servers to be ready.

    Checks TCP ports concurrently using asyncio. Hangs until all are ready.
    Caller is responsible for detecting hangs and intervening.
    """
    import asyncio

    ports = [port for _, port in AGENTS.values()]

    async def _port_ready(port: int) -> bool:
        """Wait for a single port to accept connections."""
        while True:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                return True
            except Exception:
                await asyncio.sleep(0.5)

    if not all(p.is_alive() for p in processes):
        return False

    results = await asyncio.gather(*(_port_ready(p) for p in ports))
    return all(results)


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
