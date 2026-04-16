"""Pipeline graph -- master pipeline using Strands GraphBuilder.

Replaces server/agents/pipeline.py. Defines the outer graph with:
  scenario → audio → timing_eval → [conditional refine → audio] → video → assembly

The timing feedback loop is capped at ~3 iterations via max_node_executions=12.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.multiagent.graph import GraphBuilder, GraphState

from agents.assembler_agent import build_assembler_agent
from agents.audio_agent import build_audio_agent
from agents.scenario_planner import build_scenario_planner
from agents.scenario_refiner import build_scenario_refiner
from agents.timing_evaluator import build_timing_evaluator
from agents.video_planner import build_video_planner

logger = logging.getLogger(__name__)


def _timing_passed(state: GraphState) -> bool:
    """Condition: timing evaluation passed."""
    results = state.results
    timing_result = results.get("timing_eval")
    if timing_result is None:
        return False
    # The timing evaluator outputs a JSON verdict with "passed" field
    result_text = str(timing_result)
    return '"passed": true' in result_text.lower() or '"passed":true' in result_text.lower()


def _timing_failed(state: GraphState) -> bool:
    """Condition: timing evaluation failed (needs refinement)."""
    return not _timing_passed(state)


def build_pipeline() -> Any:
    """Build and return the documentary production pipeline graph.

    Returns:
        A Strands Graph instance ready for execution.
    """
    builder = GraphBuilder()

    # Build agents
    scenario_planner = build_scenario_planner()
    audio_agent = build_audio_agent()
    timing_evaluator = build_timing_evaluator()
    scenario_refiner = build_scenario_refiner()
    video_planner = build_video_planner()
    assembler_agent = build_assembler_agent()

    # Add nodes
    builder.add_node(scenario_planner, "scenario")
    builder.add_node(audio_agent, "audio")
    builder.add_node(timing_evaluator, "timing_eval")
    builder.add_node(scenario_refiner, "refine")
    builder.add_node(video_planner, "video")
    builder.add_node(assembler_agent, "assembly")

    # Forward path
    builder.add_edge("scenario", "audio")
    builder.add_edge("audio", "timing_eval")

    # Conditional: timing OK → video
    builder.add_edge("timing_eval", "video", condition=_timing_passed)

    # Conditional: timing violations → refine
    builder.add_edge("timing_eval", "refine", condition=_timing_failed)

    # After refinement → re-generate audio for changed scenes
    builder.add_edge("refine", "audio")

    # Video → Assembly
    builder.add_edge("video", "assembly")

    # Set entry point
    builder.set_entry_point("scenario")

    # Cap the timing feedback loop at ~3 iterations
    # scenario→audio→eval→refine→audio→eval = 6 node executions per loop
    builder.set_max_node_executions(12)

    # 2-hour total timeout for the pipeline
    builder.set_execution_timeout(7200)

    # Reset node state on revisit so the feedback loop starts fresh
    builder.reset_on_revisit(True)

    pipeline = builder.build()
    logger.info("pipeline built with 6 nodes, timing feedback loop enabled")
    return pipeline
