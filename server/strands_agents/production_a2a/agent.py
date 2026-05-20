"""Production A2A Agent — conversational Strands agent for video generation.

Receives a "produce clips" message, acts as an autonomous LLM,
and provisions Vast VMs, generates videos, and writes clips to the timeline organically.
"""

from __future__ import annotations
from strands_agents.config import DEFAULT_MODEL

import logging
import time
from pathlib import Path
from typing import Any

from strands import Agent
from strands import tool as strands_tool

from ..shared_a2a.otio_tools import make_otio_tools
from ..shared_a2a.sync_otio_client import SyncOtioClient
from ..shared_a2a.vast_provisioning import make_vast_tools

logger = logging.getLogger(__name__)

def build_production_agent(
    otio_agent_url: str = "http://localhost:9001",
    model_id: str = DEFAULT_MODEL,
    run_dir: Path | str = "/tmp",
) -> Agent:
    """Build a conversational Strands agent for production."""
    otio = SyncOtioClient(otio_agent_url=otio_agent_url)
    
    @strands_tool
    def check_environment() -> str:
        """Check this agent's full environment."""
        from ..shared_a2a.env_check import build_env_check
        return build_env_check(
            "production-agent",
            otio_url=otio_agent_url,
            needs_llm=True,
            needs_ffmpeg=False,
            needs_vast=True,
            needs_worker=True,
        )

    @strands_tool
    def dispatch_render(
        scene_num: int,
        prompt: str,
        negative_prompt: str,
        duration_sec: float,
        worker_url: str,
    ) -> str:
        """Dispatch a render job to a running LTX-2.3 worker.
        Returns the path to the completed MP4 clip.
        """
        # Placeholder for actual render dispatch logic
        # In a real run, this hits the fastAPI worker running on Vast
        time.sleep(2)  # Simulate render
        return f"/tmp/renders/scene_{scene_num}_render.mp4"

    @strands_tool
    def mark_production_complete() -> str:
        """Call this when all clips are rendered and added to the timeline."""
        return "Production complete."

    otio_tools = make_otio_tools(
        otio_agent_url=otio_agent_url,
        agent_id="production-agent",
        stage="production",
    )
    
    _provisioned_vm_ids = set()
    vast_tools = make_vast_tools(
        agent_name="production-agent",
        default_worker_mode="ltx",
        label_prefix="documentary-video-",
        provisioned_vm_ids=_provisioned_vm_ids,
    )

    agent = Agent(
        model=model_id,
        name="production-agent",
        description="Handles rendering and VM provisioning autonomously.",
        tools=[check_environment, dispatch_render, mark_production_complete] + otio_tools + list(vast_tools),
        system_prompt=(
            "You are the Production Director. Your job is to orchestrate Vast.ai rendering.\n\n"
            "1. Read the timeline to see what scenes exist.\n"
            "2. Read the 'visual_concepts' metadata to get the prompt for each scene.\n"
            "3. Intelligently provision GPU VMs using search_gpu_offers and provision_vm.\n"
            "   IMPORTANT: You must implement intelligent exponential scaling.\n"
            "   Start by provisioning a single VM to verify the environment works.\n"
            "   If the first VM boots successfully and renders without issue, THEN escalate your fleet.\n"
            "   Double your fleet size (1 -> 2 -> 4 -> 8 -> 16, up to 20) only when things are running smoothly to parallelize the remaining renders.\n"
            "   Do NOT throw more VMs at the problem if the initial instances are failing to boot.\n"
            "4. Monitor VM health with check_worker_health.\n"
            "5. Dispatch renders to healthy VMs using dispatch_render.\n"
            "6. Insert the rendered clips into the timeline using add_clip_with_guard.\n"
            "7. Terminate ALL VMs when done to save money.\n"
            "8. Call mark_production_complete.\n\n"
            "You are an autonomous LLM. Do not output JSON or XML. Just use your tools."
        ),
    )

    return agent
