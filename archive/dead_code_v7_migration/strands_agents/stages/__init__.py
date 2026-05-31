"""Strands Agent stage modules — all 5 documentary pipeline stages.

Each stage module exports tools used by the graph pipeline in
:mod:`strands_agents.graph_pipeline`.

Preflight (stage 0) is not a graph node — it runs as a gate in
run_strands.py before the graph is built. See strands_agents.stages.preflight.

Stage modules:
  - scenario_stage: Scene planning tools.
  - audio_stage: Narration alignment and OTIO persistence tools.
  - visual_stage: Visual concept generation tools.
  - production_stage: Production plan generation and evaluation tools.
  - assembly_stage: Final assembly tools.
"""

from __future__ import annotations

from strands_agents.stages.scenario_stage import build_scenario_agent
from strands_agents.stages.visual_stage import build_visual_agent
from strands_agents.stages.assembly_stage import build_assembly_agent

__all__ = ['annotations', 'build_assembly_agent', 'build_scenario_agent', 'build_visual_agent']
