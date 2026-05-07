"""Strands Agent stage modules — all 6 documentary pipeline stages.

Each stage module exports a ``build_*_agent()`` function that returns a
configured :class:`strands.Agent` ready for insertion into the
documentary pipeline Graph (see :mod:`strands_agents.graph_pipeline`).

Stage modules:
  - preflight: Verify all pipeline resources before any work begins.
    Checks API keys, worker URLs, disk space, dependencies.
  - scenario_stage: Replaces the ADK LoopAgent ``scenario_director``
    with a Strands Agent + Graph backward edge for timing loop.
  - audio_stage: Replaces the ADK ``audio_agent`` with a Strands
    Agent that runs the deterministic audio pipeline.
  - visual_stage: Replaces the ADK LoopAgent ``visual_director`` with
    a Strands Agent + Graph backward edges for the coherence loop.
  - production_stage: Replaces the ADK BaseAgent ``ProductionAgent``
    with a Strands Agent wrapping the ProductionOrchestrator.
  - assembly_stage: Replaces the ADK ``assembler_agent`` with a
    Strands Agent for final assembly.
"""

from __future__ import annotations

from strands_agents.stages.preflight import build_preflight_agent
from strands_agents.stages.scenario_stage import build_scenario_agent
from strands_agents.stages.audio_stage import build_audio_agent
from strands_agents.stages.visual_stage import build_visual_agent
from strands_agents.stages.production_stage import build_production_agent
from strands_agents.stages.assembly_stage import build_assembly_agent

__all__ = [
    "build_preflight_agent",
    "build_scenario_agent",
    "build_audio_agent",
    "build_visual_agent",
    "build_production_agent",
    "build_assembly_agent",
]
