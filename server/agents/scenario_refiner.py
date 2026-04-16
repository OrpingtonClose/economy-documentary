"""Scenario refiner agent -- revises scenes based on timing violations.

Takes timing violations from the evaluator and revises only the affected
scenes, marking them with '_changed': true for incremental re-generation.
"""

from __future__ import annotations

import logging

from strands import Agent
from strands.agent.conversation_manager.summarizing_conversation_manager import SummarizingConversationManager

from agents.model_config import build_model
from agents.scenario_planner import save_scenario
from plugins.concurrency_plugin import ConcurrencyPlugin
from plugins.dashboard_plugin import DashboardPlugin
from tools.environment_tools import estimate_tts_duration, validate_plan
from tools.otio_tools import get_narration_durations_by_scene

logger = logging.getLogger(__name__)

SCENARIO_REFINER_PROMPT = """\
You are a scenario refiner. You receive timing violations from the evaluator.

Revise ONLY the affected scenes to fix timing issues while preserving narrative
quality. For each revised scene:
- Shorten or restructure narration text to fit within duration budgets
- Verify new text with estimate_tts_duration before committing
- Run validate_plan on the revised scenes
- Mark revised scenes with '_changed': true so only they get re-generated audio

Do NOT change scenes that have no timing violations. Preserve the overall
narrative arc and documentary structure.

MANDATORY: After revising scenes, call save_scenario with the complete revised
scenes JSON array. This updates the shared pipeline state so the audio agent
re-generates only the changed scenes on the next loop iteration. If you skip
this step, the audio agent will use the old scenes and the revisions will be lost.
"""


def build_scenario_refiner() -> Agent:
    """Build and return the scenario refiner agent."""
    return Agent(
        name="scenario_refiner",
        system_prompt=SCENARIO_REFINER_PROMPT,
        model=build_model(),
        tools=[
            save_scenario,
            estimate_tts_duration,
            validate_plan,
            get_narration_durations_by_scene,
        ],
        plugins=[ConcurrencyPlugin(), DashboardPlugin()],
        conversation_manager=SummarizingConversationManager(
            summary_ratio=0.3,
            preserve_recent_messages=10,
        ),
    )
