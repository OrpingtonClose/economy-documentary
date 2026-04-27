"""Regression tests for ``_TOOL_TO_STAGE`` in the live pipeline runner.

The mapping must include every ``@tool`` exposed by the production
orchestrator. A missing entry silently breaks the stage ribbon: the
runner's :class:`_StageTracker` would not transition to the right
stage when the orchestrator calls a known tool, and the tool's events
would never appear under any stage bracket on the UI.

These tests pin the mapping against the canonical tool set so a future
slice that adds a new ``@tool`` cannot ship without also extending
``_TOOL_TO_STAGE``.
"""

from __future__ import annotations

import pytest

from strands_agents.playground import pipeline_live_runner as runner


class TestToolToStageCoverage:
    """``_TOOL_TO_STAGE`` covers every orchestrator tool."""

    @pytest.mark.parametrize(
        ("tool_name", "expected_stage"),
        [
            ("generate_scenario", "scenario"),
            ("refine_scenario", "scenario"),
            ("evaluate_scenario", "scenario"),
            ("evaluate_timing", "audio"),
            ("launch_audio_render", "audio"),
            ("qa_audio_completeness", "audio"),
            ("content_analyst", "visual"),
            ("visual_concepter", "visual"),
            ("propose_visual_concept", "visual"),
            ("launch_visual_production", "production"),
            ("qa_video_artifact_probe", "production"),
            ("qa_duration_align", "production"),
            ("qa_stills_judge", "production"),
            ("launch_assembly", "assembly"),
            ("launch_b2_sync", "assembly"),
        ],
    )
    def test_known_tool_maps_to_expected_stage(
        self,
        tool_name: str,
        expected_stage: str,
    ) -> None:
        assert runner._stage_for_tool(tool_name) == expected_stage

    @pytest.mark.parametrize(
        "tool_name",
        ["check_tasks", "await_tasks", "request_human_approval"],
    )
    def test_stage_neutral_tools_return_none(self, tool_name: str) -> None:
        """Stage-neutral tools never transition the stage bracket."""
        assert runner._stage_for_tool(tool_name) is None

    def test_unknown_tool_returns_none(self) -> None:
        """Unknown tools return ``None`` rather than raising."""
        assert runner._stage_for_tool("not_a_real_tool") is None


class TestProposeVisualConceptStage:
    """slice 9c-LLM-visual added ``propose_visual_concept``; pin its stage.

    Without this entry the visual stage bracket never opens for runs
    where the LLM calls ``propose_visual_concept`` instead of (or
    before) the legacy ``content_analyst`` / ``visual_concepter``
    placeholders.
    """

    def test_propose_visual_concept_in_mapping(self) -> None:
        assert "propose_visual_concept" in runner._TOOL_TO_STAGE

    def test_propose_visual_concept_is_visual(self) -> None:
        assert runner._TOOL_TO_STAGE["propose_visual_concept"] == "visual"

    def test_propose_visual_concept_groups_with_visual_siblings(self) -> None:
        """All three visual tools land in the same bracket."""
        for sibling in ("content_analyst", "visual_concepter"):
            assert (
                runner._TOOL_TO_STAGE[sibling]
                == runner._TOOL_TO_STAGE["propose_visual_concept"]
            )
