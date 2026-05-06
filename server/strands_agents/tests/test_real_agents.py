"""Test that the Graph builds with real stage agents (not placeholders)."""

from __future__ import annotations

import pytest

from strands_agents.graph_pipeline import (
    build_documentary_graph,
    SCENARIO,
    AUDIO,
    VISUAL,
    PRODUCTION,
    ASSEMBLY,
)
from strands_agents.otio_manager import OTIOStateManager


class TestGraphWithRealAgents:
    """Graph builds with real Strands Agent stages."""

    def test_graph_uses_real_scenario_agent(self):
        """Graph builds with the real scenario agent."""
        graph = build_documentary_graph()
        executor = graph.nodes[SCENARIO].executor
        assert executor.name == SCENARIO
        # The real agent has tools (not empty like a placeholder)
        assert len(executor.tool_names) > 0

    def test_graph_uses_real_audio_agent(self):
        """Graph builds with the real audio agent."""
        graph = build_documentary_graph()
        executor = graph.nodes[AUDIO].executor
        assert executor.name == AUDIO
        assert len(executor.tool_names) > 0

    def test_graph_uses_real_visual_agent(self):
        """Graph builds with the real visual agent."""
        graph = build_documentary_graph()
        executor = graph.nodes[VISUAL].executor
        assert executor.name == "visual_director"
        assert len(executor.tool_names) > 0

    def test_graph_uses_real_production_agent(self):
        """Graph builds with the real production agent."""
        graph = build_documentary_graph()
        executor = graph.nodes[PRODUCTION].executor
        assert executor.name == "production_supervisor"
        assert len(executor.tool_names) > 0

    def test_graph_uses_real_assembly_agent(self):
        """Graph builds with the real assembly agent."""
        graph = build_documentary_graph()
        executor = graph.nodes[ASSEMBLY].executor
        assert executor.name == "assembler_agent"
        assert len(executor.tool_names) > 0

    def test_graph_with_otio_manager(self, tmp_path):
        """Graph builds with OTIOStateManager and real agents."""
        mgr = OTIOStateManager(output_dir=str(tmp_path))
        mgr.create_timeline("test_graph")
        graph = build_documentary_graph(otio_manager=mgr)
        assert len(graph.nodes) == 5

    def test_all_real_agents_have_tools(self):
        """All real stage agents have at least one tool."""
        graph = build_documentary_graph()
        for stage in [SCENARIO, AUDIO, VISUAL, PRODUCTION, ASSEMBLY]:
            executor = graph.nodes[stage].executor
            assert len(executor.tool_names) > 0, f"Stage {stage} has no tools"
