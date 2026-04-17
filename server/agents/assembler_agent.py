"""
Assembler Agent -- final assembly of video + audio into the documentary.

Reads the OTIO timeline, trims video clips to match audio durations,
muxes audio and video for each scene, then concatenates all scenes
into the final documentary output.
"""

from __future__ import annotations

import logging

from google.adk.agents import Agent

from agents.model_config import build_model
from callbacks.deterministic_steps import deterministic_assembly_callback
from callbacks.timeline_guardian import timeline_guardian_callback

logger = logging.getLogger(__name__)

_INSTRUCTION = """\
You are the Assembler Agent for a documentary pipeline.
Assembly is handled automatically. Report completion.
"""


assembler_agent = Agent(
    name="assembler_agent",
    model=build_model(),
    instruction=_INSTRUCTION,
    tools=[],
    before_agent_callback=deterministic_assembly_callback,
    after_agent_callback=timeline_guardian_callback,
)
