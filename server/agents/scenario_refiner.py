"""
Scenario Refiner — adjusts scene durations when timing evaluation fails.

This agent is part of the timing feedback loop (R3 from the deep audit).
It runs after the timing evaluator and ONLY activates when
state["timing_passed"] is False.  When timing passes, the
before_agent_callback returns Content to skip the LLM entirely.

The refiner reads the timing_analysis from state and adjusts the
narration text lengths to bring the total duration within budget.

Architecture (inside the timing_loop LoopAgent)::

    LoopAgent("timing_loop", max_iterations=3)
    ├── Agent("audio_agent")          # TTS + WhisperX
    ├── Agent("timing_evaluator")     # checks duration budget
    └── Agent("scenario_refiner")     # THIS — adjusts scenes if needed
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.exit_loop_tool import exit_loop
from google.genai import types as genai_types

from agents.model_config import build_model
from callbacks.before_model import before_model_callback
from callbacks.after_model import after_model_callback

logger = logging.getLogger(__name__)


def _skip_if_timing_passed(callback_context: CallbackContext) -> Optional[genai_types.Content]:
    """Skip the refiner entirely if timing already passed.

    This is the key optimization: when timing is within budget, the
    refiner returns Content immediately (skipping the LLM call) and
    calls exit_loop to break out of the timing_loop LoopAgent.

    When timing fails, returns None so the LLM runs and adjusts scenes.
    """
    state = callback_context.state
    timing_passed = state.get("timing_passed", False)

    # Handle string "True"/"False" from state serialization
    if isinstance(timing_passed, str):
        timing_passed = timing_passed.lower() in ("true", "1", "yes")

    if timing_passed:
        logger.info("Timing passed — skipping scenario refiner, exiting loop")
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(
                text="TIMING PASSED — no refinement needed. Exiting timing loop."
            )],
        )

    logger.info("Timing FAILED — scenario refiner will adjust scenes")
    return None


def _save_refined_scenes(callback_context: CallbackContext) -> None:
    """After refiner: persist refined scenes to state and backup.

    The refiner outputs adjusted scenes as JSON.  We parse and persist
    them so the next audio generation iteration uses the refined text.
    """
    from callbacks.deterministic_steps import extract_json_array

    state = callback_context.state
    raw = state.get("scenes", "")
    scenes = extract_json_array(str(raw)) if raw else None

    if scenes:
        # Persist backup so we don't lose refined scenes on re-run
        state["_approved_scenes_backup"] = json.dumps(scenes)
        logger.info(
            "Refined scenes saved: %d scenes", len(scenes),
        )

        # Clear timing state so audio regeneration is fresh
        state["timing_passed"] = False
        state.pop("timing_analysis", None)

        # Clear audio state to force regeneration with new scenes
        # The audio callback checks for existing clips and skips them,
        # so we need to signal that audio should be regenerated.
        state["_audio_needs_regeneration"] = True

    return None


_REFINER_INSTRUCTION = """\
You are the Scenario Refiner for a documentary timing feedback loop.

The timing evaluation FAILED. You must adjust the scene narration text
to bring the total duration within budget.

TIMING ANALYSIS:
{timing_analysis}

CURRENT SCENES:
{scenes}

YOUR TASK:
1. Read the timing analysis to understand which scenes are over/under budget
2. For OVER-BUDGET scenes: shorten the narration text (fewer words per voice block)
   while preserving the key information and narrative arc
3. For UNDER-BUDGET scenes: slightly expand the narration if the total is under target
4. Keep the same scene structure (scene_num, title, voices V1/V2/V3)
5. Keep visual_notes and dopamine_hook unchanged
6. Maintain the same tone and quality — just adjust length

CRITICAL RULES:
- Target ~2.5 words per second of narration (150 words/minute)
- Each voice block should have natural sentence boundaries
- Do NOT add or remove scenes — only adjust text length
- Do NOT change visual_notes, dopamine_hook, or scene titles
- Output the full revised scenes array as valid JSON

After adjusting, output the complete scenes JSON array.
Then call exit_loop() to signal refinement is complete.

The audio agent will regenerate narration with your revised text on the
next iteration of the timing loop.
"""


scenario_refiner = Agent(
    name="scenario_refiner",
    model=build_model(synthesis=True),
    instruction=_REFINER_INSTRUCTION,
    tools=[exit_loop],
    output_key="scenes",
    before_agent_callback=_skip_if_timing_passed,
    after_agent_callback=_save_refined_scenes,
    before_model_callback=before_model_callback,
    after_model_callback=after_model_callback,
)
