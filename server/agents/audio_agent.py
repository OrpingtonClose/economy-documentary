"""
Audio Agent -- TTS generation + WhisperX alignment.

Generates TTS for all scenes (3 voices per scene) using Qwen3-TTS,
then runs WhisperX alignment on generated audio.
Writes alignment data to state["whisperx_alignment"].

NOTE: The actual work is done deterministically in the before_agent_callback
(deterministic_audio_callback) to avoid unreliable LLM tool-calling.
The LLM agent is skipped entirely — the callback returns Content directly.
"""

from __future__ import annotations

import logging

from google.adk.agents import Agent

from agents.model_config import build_model
from callbacks.deterministic_steps import deterministic_audio_callback
from callbacks.timeline_guardian import timeline_guardian_callback

logger = logging.getLogger(__name__)

_INSTRUCTION = """\
You are the Audio Agent for a documentary pipeline.
Audio generation is handled automatically. Report completion.
"""


audio_agent = Agent(
    name="audio_agent",
    model=build_model(),
    instruction=_INSTRUCTION,
    tools=[],
    output_key="whisperx_alignment",
    before_agent_callback=deterministic_audio_callback,
    after_agent_callback=timeline_guardian_callback,
)
