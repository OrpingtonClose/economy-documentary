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
from google.genai import types as genai_types

from agents.model_config import build_model
from callbacks.before_model import before_model_callback
from callbacks.after_model import after_model_callback

logger = logging.getLogger(__name__)


def _skip_if_timing_passed(callback_context: CallbackContext) -> Optional[genai_types.Content]:
    """Skip the refiner entirely if timing already passed.

    This is the key optimization: when timing is within budget, the
    refiner returns Content immediately (skipping the LLM call) AND
    sets actions.escalate=True so the parent LoopAgent exits.

    Without actions.escalate, LoopAgent would run all max_iterations
    even though timing passed on iteration 1 — wasting GPU time on
    redundant TTS generation + WhisperX alignment per extra iteration.

    When timing fails, returns None so the LLM runs and adjusts scenes.
    """
    state = callback_context.state
    timing_passed = state.get("timing_passed", False)

    # Handle string "True"/"False" from state serialization
    if isinstance(timing_passed, str):
        timing_passed = timing_passed.lower() in ("true", "1", "yes")

    if timing_passed:
        logger.info("Timing passed — skipping scenario refiner, exiting loop")
        # Signal LoopAgent to exit: LoopAgent checks event.actions.escalate
        # after each sub-agent and breaks when True.  This is the same
        # mechanism used by the exit_loop tool (which sets
        # tool_context.actions.escalate = True).
        callback_context.actions.escalate = True
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
        # Persist cleaned JSON back to state AND backup.
        # Without this, output_key="scenes" stores the raw LLM output
        # (which may include markdown fences, explanatory text, etc.).
        # On subsequent iterations, {scenes} in the instruction expands
        # to this raw output, nesting previous iterations' noise and
        # degrading LLM response quality.
        cleaned_json = json.dumps(scenes, ensure_ascii=False)
        state["scenes"] = cleaned_json
        state["_approved_scenes_backup"] = cleaned_json
        logger.info(
            "Refined scenes saved: %d scenes", len(scenes),
        )

        # Clear timing state so audio regeneration is fresh
        state["timing_passed"] = False
        state["timing_analysis"] = None

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

Do NOT try to exit the loop — the timing loop will automatically
re-generate audio with your revised text on the next iteration.
The loop exits only when the timing evaluator confirms the audio
is within budget.
"""


scenario_refiner = Agent(
    name="scenario_refiner",
    model=build_model(synthesis=True),
    instruction=_REFINER_INSTRUCTION,
    tools=[],
    output_key="scenes",
    before_agent_callback=_skip_if_timing_passed,
    after_agent_callback=_save_refined_scenes,
    before_model_callback=before_model_callback,
    after_model_callback=after_model_callback,
)
