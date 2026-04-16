"""Scenario planner agent -- the core planning agent for documentary production.

Replaces server/agents/scenario_director.py. Uses a single powerful Strands Agent
with rich toolset and AgentSkills for technique guidance.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from strands import Agent, tool
from strands.agent.conversation_manager.summarizing_conversation_manager import SummarizingConversationManager
from strands.vended_plugins.skills import AgentSkills

from agents.model_config import build_model
from plugins.concurrency_plugin import ConcurrencyPlugin
from plugins.dashboard_plugin import DashboardPlugin
from tools.environment_tools import (
    estimate_tts_duration,
    get_worker_fleet_status,
    query_gatekeeper_rules,
    query_production_capabilities,
    query_voice_profiles,
    validate_plan,
)
from tools.lora_tools import query_lora_catalog
from tools.otio_tools import create_timeline, get_timeline_status

logger = logging.getLogger(__name__)

_SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills")


@tool
def read_corpus(corpus_path: str) -> str:
    """Read the research corpus file for documentary planning.

    Args:
        corpus_path: Path to the corpus markdown file.

    Returns:
        The corpus text content.
    """
    if not os.path.isfile(corpus_path):
        return f"Error: corpus file not found at {corpus_path}"
    with open(corpus_path) as f:
        return f.read()


@tool
def save_scenario(scenes_json: str, visual_style_json: str = "", tool_context=None) -> str:
    """Persist the documentary scenario to shared pipeline state.

    MUST be called after generating the scene plan. Writes scenes and
    visual_style to invocation_state so downstream agents (audio, video,
    assembly) can access them via the shared state dict.

    Args:
        scenes_json: JSON array of scene objects.
        visual_style_json: Optional JSON object with movie-level visual style.

    Returns:
        Confirmation message with scene count.
    """
    if tool_context is None:
        return "Error: tool_context not available, cannot persist scenario"

    state = tool_context.invocation_state

    # Parse and re-serialize to validate and normalize
    from callbacks.deterministic_steps import extract_json_array

    scenes = extract_json_array(scenes_json)
    if scenes:
        state["scenes"] = json.dumps(scenes, ensure_ascii=False)
        logger.info("save_scenario: persisted %d scenes to pipeline state", len(scenes))
    else:
        # Fall back to raw text so deterministic callbacks can try harder to parse
        state["scenes"] = scenes_json
        logger.warning("save_scenario: could not parse scenes JSON, saved raw text")

    if visual_style_json and visual_style_json.strip():
        state["visual_style"] = visual_style_json

    # Upload scenario to B2 checkpoint if available
    try:
        from tools.b2_checkpoint import upload_scenario as _b2_upload
        _b2_upload(state.get("scenes", "[]"), state.get("visual_style", "{}"))
    except (ImportError, Exception) as exc:
        logger.debug("B2 scenario upload skipped: %s", exc)

    count = len(scenes) if scenes else "unknown"
    return f"Saved {count} scenes to pipeline state. Downstream agents can now access them."


SCENARIO_PLANNER_PROMPT = """\
You are a documentary planner. Create a compelling documentary from the provided corpus.

Before designing scenes, use your tools to understand the production environment:
- What video generation model is available and its limits
- What TTS voices exist and their characteristics
- What validation rules apply to the scenario

Design scenes that work within the realities you discover. Test your assumptions
with estimate_tts_duration before committing to narration text lengths.

Check your available_skills for technique guidance on cinematography, voice design,
visual storytelling, and ADHD-friendly documentary format.

Each scene must have:
- scene_num (int)
- title (str)
- voice_blocks: [{voice: "V1"|"V2"|"V3", text: str, visual_notes: str}]
- dopamine_hook (str) - attention grabber for the first 3 seconds
- visual_style: {mood: str, palette: str, avoid: str}
- lora_id (str) - from the LoRA catalog

IMPORTANT WORKFLOW:
1. Use read_corpus to load the source material
2. Use query_production_capabilities, query_voice_profiles, query_gatekeeper_rules
   to discover constraints
3. Design your scenes array
4. Use create_timeline to initialize the OTIO timeline
5. MANDATORY: Call save_scenario with the scenes JSON array and a visual_style
   JSON object. This persists the plan to shared pipeline state so audio and
   video agents can access it. If you skip this step, downstream agents will
   have no scenes to work with and the pipeline will fail.
"""


def build_scenario_planner() -> Agent:
    """Build and return the scenario planner agent."""
    technique_skills = AgentSkills(skills=[_SKILLS_DIR])

    return Agent(
        name="scenario_planner",
        system_prompt=SCENARIO_PLANNER_PROMPT,
        model=build_model(),
        tools=[
            read_corpus,
            save_scenario,
            query_production_capabilities,
            estimate_tts_duration,
            validate_plan,
            query_gatekeeper_rules,
            query_voice_profiles,
            get_worker_fleet_status,
            create_timeline,
            get_timeline_status,
            query_lora_catalog,
        ],
        plugins=[technique_skills, ConcurrencyPlugin(), DashboardPlugin()],
        conversation_manager=SummarizingConversationManager(
            summary_ratio=0.3,
            preserve_recent_messages=10,
        ),
    )
