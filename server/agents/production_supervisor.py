"""
Production Supervisor -- orchestrates actual video generation on GPU.

Reads visual concepts from state["visual_concepts"], provisions GPU VMs
via Vast.ai, generates video clips using LTX-2.3, probes results, and
adds clips to the OTIO timeline.

Uses the escalate pattern from MiroThinker's thinker: monitors progress
and signals completion when all clips are generated and validated.
"""

from __future__ import annotations

import logging

from google.adk.agents import Agent

from agents.model_config import build_model
from callbacks.deterministic_steps import deterministic_production_callback
from callbacks.timeline_guardian import timeline_guardian_callback

logger = logging.getLogger(__name__)

_INSTRUCTION = """\
You are the Production Supervisor for a documentary pipeline.
Video generation is handled automatically. Report completion.
"""


production_supervisor = Agent(
    name="production_supervisor",
    model=build_model(),
    instruction=_INSTRUCTION,
    tools=[],
    before_agent_callback=deterministic_production_callback,
    after_agent_callback=timeline_guardian_callback,
)
