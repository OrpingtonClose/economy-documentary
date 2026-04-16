"""Audio Agent -- TTS generation + WhisperX alignment.

Generates TTS for all scenes (3 voices per scene) using Qwen3-TTS,
then runs WhisperX alignment on generated audio.
Writes alignment data to invocation_state["whisperx_alignment"].

The actual work is done deterministically via generate_all_narration tool
to avoid unreliable LLM tool-calling. Supports incremental re-generation
for scenes marked with '_changed': true.
"""

from __future__ import annotations

import logging

from strands import Agent, tool

from agents.model_config import build_model
from plugins.concurrency_plugin import ConcurrencyPlugin
from plugins.dashboard_plugin import DashboardPlugin
from plugins.timeline_guardian_plugin import TimelineGuardianPlugin

logger = logging.getLogger(__name__)


@tool
def generate_all_narration(tool_context=None) -> str:
    """Run deterministic TTS generation and WhisperX alignment for all scenes.

    Reads scenes from invocation_state, generates narration for each voice block,
    runs WhisperX alignment, and writes results back to invocation_state.
    Supports incremental re-generation: if a scene has '_changed': true,
    only that scene's audio is regenerated.

    Returns:
        Status summary of the narration generation.
    """
    from callbacks.deterministic_steps import deterministic_audio_callback

    # The deterministic callback expects a CallbackContext-like object.
    # We adapt the invocation_state to work with it.
    state = tool_context.invocation_state if tool_context else {}

    # Wrap state in StateDictProxy which delegates to the original dict by
    # reference (no copying) and adds .to_dict() for ADK compatibility.
    from callbacks._compat import StateDictProxy

    class _StateAdapter:
        def __init__(self, s: dict) -> None:
            self.state = s if isinstance(s, StateDictProxy) else StateDictProxy(s)

    adapter = _StateAdapter(state)

    try:
        result = deterministic_audio_callback(adapter)
        if result is not None:
            return f"Audio generation complete. Result: {result}"
        return "Audio generation complete."
    except Exception as e:
        logger.exception("audio generation failed")
        return f"Audio generation failed: {e}"


_INSTRUCTION = """\
You are the Audio Agent for a documentary pipeline.
Call generate_all_narration to run TTS generation and WhisperX alignment
for all scenes. Report completion with a summary of generated audio files.
"""


def build_audio_agent() -> Agent:
    """Build and return the audio agent."""
    return Agent(
        name="audio_agent",
        system_prompt=_INSTRUCTION,
        model=build_model(),
        tools=[generate_all_narration],
        plugins=[
            ConcurrencyPlugin(),
            DashboardPlugin(),
            TimelineGuardianPlugin(),
        ],
    )
