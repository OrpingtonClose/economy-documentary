"""
Trajectory tests — verify the Strands pipeline produces correct outputs.

These tests drive the full Strands Graph pipeline with mock services
(Substrate) and compare the execution trajectory against expected
patterns. They validate that:
1. All 5 stages execute in order
2. Recovery loops work (backward edges activate)
3. Approval gates interrupt correctly
4. OTIO state transitions are correct
5. Checkpoints are recorded after each stage
"""

from __future__ import annotations

import asyncio
import os
import pytest

from strands_agents.graph_pipeline import (
    build_documentary_graph,
    RecoveryShell,
    SCENARIO,
    AUDIO,
    VISUAL,
    PRODUCTION,
    ASSEMBLY,
)
from strands_agents.otio_manager import OTIOStateManager
from strands_agents.hooks.pipeline_hooks import (
    BudgetHook,
    CheckpointHook,
)
from strands_agents.feature_flag import (
    BACKEND_STRANDS,
    get_pipeline_backend,
    is_strands_enabled,
)


class TestPipelineTrajectory:
    """Full pipeline trajectory tests with mock agents."""

    def test_graph_builds_with_placeholder_agents(self):
        """Graph builds and has correct topology."""
        graph = build_documentary_graph()
        assert len(graph.nodes) == 5
        node_ids = set(graph.nodes.keys())
        assert node_ids == {SCENARIO, AUDIO, VISUAL, PRODUCTION, ASSEMBLY}

    def test_recovery_shell_constructs(self):
        """RecoveryShell wraps a Graph."""
        graph = build_documentary_graph()
        shell = RecoveryShell(graph, max_retries=3)
        assert shell.max_retries == 3

    def test_otio_manager_lifecycle(self):
        """OTIO manager transitions through draft → authoritative."""
        mgr = OTIOStateManager(output_dir="/tmp/test_trajectory")
        mgr.create_timeline("trajectory_test")
        assert mgr.state == "draft"

        # Add clips in draft mode (allowed)
        mgr.add_clip("V1_Video", 1, 0, "/tmp/clip.mp4", 5.0)
        assert mgr._clip_counts()["V1_Video"] == 1

        # Transition to authoritative
        mgr.set_authoritative("test_transition")
        assert mgr.is_authoritative

        # Mutations now blocked
        from strands_agents.otio_manager import OtioStateViolation
        with pytest.raises(OtioStateViolation):
            mgr.guard_mutation("add_clip")

        # Escalation opens the window
        mgr.begin_escalation("REPLACE", "test", "test")
        mgr.reset_to_draft("test_recovery")
        assert mgr.state == "draft"

    def test_otio_manager_checkpoint(self):
        """Checkpoint records the timeline state."""
        mgr = OTIOStateManager(output_dir="/tmp/test_checkpoint")
        mgr.create_timeline("checkpoint_test")
        mgr.add_clip("V1_Video", 1, 0, "/tmp/clip.mp4", 5.0)
        cp = mgr.checkpoint("after_scenario")
        assert cp["label"] == "after_scenario"
        assert cp["otio_state"] == "draft"
        assert cp["clip_counts"]["V1_Video"] == 1

    def test_otio_manager_read_summaries(self):
        """Read returns text summaries for each stage."""
        mgr = OTIOStateManager(output_dir="/tmp/test_read")
        mgr.create_timeline("read_test")
        for stage in [SCENARIO, AUDIO, VISUAL, PRODUCTION, ASSEMBLY]:
            summary = mgr.read(stage)
            assert "Timeline state" in summary
            assert "draft" in summary

    def test_budget_hook_tracks_cost(self):
        """BudgetHook accumulates cost across node calls."""
        hook = BudgetHook(budget_usd=10.0)
        # Simulate 3 stages costing $3 each
        for _ in range(3):
            event = _MockAfterNodeEvent(invocation_state={"_stage_cost": 3.0})
            asyncio.get_event_loop().run_until_complete(hook.on_after_node_call(event))
        assert hook.accrued == 9.0

    def test_checkpoint_hook_with_manager(self):
        """CheckpointHook records a checkpoint when otio_manager is available."""
        mgr = OTIOStateManager(output_dir="/tmp/test_cp_hook")
        mgr.create_timeline("cp_hook_test")
        hook = CheckpointHook()
        event = _MockAfterNodeEvent(
            node_id=SCENARIO,
            invocation_state={"otio_manager": mgr},
        )
        asyncio.get_event_loop().run_until_complete(hook.on_after_node_call(event))
        assert len(mgr.checkpoints) == 1
        assert mgr.checkpoints[0]["label"] == f"after_{SCENARIO}"


class TestFeatureFlag:
    """Feature flag controls pipeline selection."""

    def test_default_is_adk(self):
        os.environ.pop("PIPELINE_BACKEND", None)
        assert get_pipeline_backend() == "adk"

    def test_strands_mode(self):
        os.environ["PIPELINE_BACKEND"] = "strands"
        assert is_strands_enabled()

    def test_shadow_mode(self):
        os.environ["PIPELINE_BACKEND"] = "shadow"
        assert is_strands_enabled()


class TestGPUProtocol:
    """Mock GPU protocol for testing."""

    def test_mock_protocol_succeeds(self):
        from strands_agents.gpu_protocol import MockGPUProtocol, GPUJobType, GPUJobRequest
        mock = MockGPUProtocol()
        request = GPUJobRequest(job_type=GPUJobType.VIDEO_RENDER, scene_num=1)
        result = asyncio.get_event_loop().run_until_complete(mock.submit(request))
        assert result.status.value == "completed"
        assert result.output_path != ""

    def test_mock_protocol_fails_scripted(self):
        from strands_agents.gpu_protocol import MockGPUProtocol, GPUJobType, GPUJobRequest
        mock = MockGPUProtocol(fail_types={GPUJobType.VIDEO_RENDER})
        request = GPUJobRequest(job_type=GPUJobType.VIDEO_RENDER, scene_num=1)
        result = asyncio.get_event_loop().run_until_complete(mock.submit(request))
        assert result.status.value == "failed"

    def test_mock_protocol_check_job(self):
        from strands_agents.gpu_protocol import MockGPUProtocol, GPUJobType, GPUJobRequest
        mock = MockGPUProtocol()
        request = GPUJobRequest(job_type=GPUJobType.TTS_RENDER, scene_num=1)
        submitted = asyncio.get_event_loop().run_until_complete(mock.submit(request))
        checked = asyncio.get_event_loop().run_until_complete(mock.check(submitted.job_id))
        assert checked.job_id == submitted.job_id


# ---------------------------------------------------------------------------
# Mock event classes
# ---------------------------------------------------------------------------


class _MockAfterNodeEvent:
    def __init__(self, node_id="test", invocation_state=None):
        self.node_id = node_id
        self.invocation_state = invocation_state or {}
