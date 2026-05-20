"""Scenario A2A Agent — conversational Strands agent for scenario production.

Receives a "produce scenario" message, acts as an autonomous LLM,
and writes the results directly to otio-agent via A2A messages using
the native otio_tools.
"""

from __future__ import annotations
from strands_agents.config import DEFAULT_MODEL

import logging
from typing import Any

from strands import Agent
from strands import tool as strands_tool

from ..shared_a2a.otio_tools import make_otio_tools
from ..shared_a2a.sync_otio_client import SyncOtioClient
from tools.technique_tools import query_techniques, get_technique_details, count_words, estimate_speaking_duration

logger = logging.getLogger(__name__)

def build_scenario_agent(
    otio_agent_url: str = "http://localhost:9001",
    model_id: str = DEFAULT_MODEL,
    refine_cap: int = 3,
) -> Agent:
    """Build a conversational Strands agent for scenario production.

    The agent is powered by a real conversational LLM. It uses the 
    otio_tools to write scenes directly to the otio-agent timeline,
    bypassing rigid structured parsing completely.
    """
    otio = SyncOtioClient(otio_agent_url=otio_agent_url)
    
    @strands_tool
    def check_environment() -> str:
        """Check this agent's full environment and explain readiness."""
        from ..shared_a2a.env_check import build_env_check
        return build_env_check(
            "scenario-agent",
            otio_url=otio_agent_url,
            needs_llm=True,
            needs_ffmpeg=False,
            needs_vast=False,
            needs_worker=False,
        )
        
    @strands_tool
    def mark_scenario_complete() -> str:
        """Call this tool when you have finished adding all scenes and metadata to the timeline."""
        return "Scenario production complete."

    # Expose the rich OTIO tools natively to the agent so it can call them
    otio_tools = make_otio_tools(
        otio_agent_url=otio_agent_url,
        agent_id="scenario-agent",
        stage="scenario",
    )
    
    # Also give it a direct tool to just add a scene to the scenario stage
    @strands_tool
    def add_scene(
        scene_num: int,
        title: str,
        duration_sec: float,
        narration: str,
    ) -> str:
        """Add a scene to the documentary timeline.
        
        Args:
            scene_num: The scene number (1-indexed).
            title: A short title for the scene.
            duration_sec: The estimated duration in seconds.
            narration: The spoken narration for this scene.
        """
        try:
            result = otio.add_scene(
                num=scene_num,
                text=narration,
                duration=float(duration_sec),
                stage="scenario",
                agent_id="scenario-agent",
            )
            return f"Successfully added Scene {scene_num} to the timeline."
        except Exception as e:
            return f"Failed to add scene: {e}"

    agent = Agent(
        model=model_id,
        name="scenario-agent",
        description=(
            "Scenario Director for the documentary pipeline. "
            "Produces scenes from a topic and writes them to otio-agent via A2A."
        ),
        tools=[check_environment, mark_scenario_complete, add_scene, query_techniques, get_technique_details, count_words, estimate_speaking_duration] + otio_tools,
        system_prompt=(
            "You are the Scenario Director for a documentary pipeline.\n\n"
            "When you receive a request to produce a scenario:\n"
            "1. Think creatively about how to break the topic into engaging scenes.\n"
            "2. Decide on a visual style and style lock. Use write_pipeline_data to save them under keys 'visual_style' and 'style_lock'.\n"
            "3. For EACH scene, organically call the add_scene tool with your written narration and duration.\n"
            "4. Use the count_words and estimate_speaking_duration tools to mathematically verify your pacing instead of guessing.\n"
            "5. When you are completely finished writing and adding all scenes, call mark_scenario_complete.\n\n"
            "Constraints:\n"
            "- You are a REAL conversational agent collaborating with other agents.\n"
            "- Do not output JSON. Do not output XML. Use tools to interact with the timeline.\n"
            "- Trust your own creative judgment."
        ),
    )

    return agent
