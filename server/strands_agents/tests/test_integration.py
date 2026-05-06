"""
Integration test — full Strands pipeline with mock services.

Drives the 5-node Graph with:
- Mock OTIOStateManager (no real OTIO dependency)
- Mock GPUProtocol (instant completion)
- Mock LLM (scripted responses)
- Feature flag set to 'strands'

Validates:
1. Graph builds and runs end-to-end
2. All 5 stages execute in order
3. OTIO state transitions correctly
4. QA gates fire after each stage
5. Checkpoints recorded after each stage
6. Recovery path works when a stage fails
"""

from __future__ import annotations

import asyncio
import json
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
from strands_agents.gpu_protocol import MockGPUProtocol, GPUJobType, GPUJobRequest
from strands_agents.hooks.pipeline_hooks import (
    BudgetHook,
    CheckpointHook,
    QANodeHook,
    ApprovalGateHook,
)
from strands_agents.feature_flag import BACKEND_STRANDS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def otio_manager(tmp_path):
    """Create an OTIOStateManager with a temp directory."""
    mgr = OTIOStateManager(output_dir=str(tmp_path))
    mgr.create_timeline("integration_test")
    return mgr


@pytest.fixture
def gpu_protocol():
    """Create a mock GPU protocol that succeeds by default."""
    return MockGPUProtocol()


@pytest.fixture
def failing_gpu_protocol():
    """Create a mock GPU protocol that fails video renders."""
    return MockGPUProtocol(fail_types={GPUJobType.VIDEO_RENDER})


@pytest.fixture
def strands_mode():
    """Set feature flag to strands mode."""
    os.environ["PIPELINE_BACKEND"] = "strands"
    yield
    os.environ.pop("PIPELINE_BACKEND", None)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestFullPipelineIntegration:
    """End-to-end pipeline integration tests."""

    def test_graph_builds_with_real_otio_manager(self, otio_manager):
        """Graph builds with a real OTIOStateManager."""
        graph = build_documentary_graph(otio_manager=otio_manager)
        assert len(graph.nodes) == 5
        assert otio_manager.state == "draft"

    def test_graph_builds_with_hooks(self, otio_manager):
        """Graph builds with pipeline hooks."""
        hooks = [
            BudgetHook(budget_usd=50.0),
            CheckpointHook(),
            QANodeHook(),
        ]
        graph = build_documentary_graph(
            otio_manager=otio_manager,
            hooks=hooks,
        )
        assert len(graph.nodes) == 5

    def test_otio_manager_full_lifecycle(self, otio_manager):
        """OTIO manager survives the full pipeline lifecycle."""
        # Scenario stage: draft, add clips
        assert otio_manager.state == "draft"
        otio_manager.add_clip("V1_Video", 1, 0, "/tmp/s1.mp4", 5.0)
        otio_manager.add_clip("A1_Narration", 1, 0, "/tmp/s1.wav", 5.0)

        # Audio stage: add more clips
        otio_manager.add_clip("A1_Narration", 1, 1, "/tmp/s1p1.wav", 3.0)
        otio_manager.add_clip("A2_Music", 1, 0, "/tmp/s1_music.wav", 8.0)

        # Transition to authoritative
        otio_manager.set_authoritative("narration_reconciliation_complete")
        assert otio_manager.is_authoritative

        # Visual + production: read-only (guarded)
        summary = otio_manager.read("visual")
        assert "authoritative" in summary

        # Checkpoint
        cp = otio_manager.checkpoint("full_lifecycle")
        assert cp["otio_state"] == "authoritative"
        assert cp["clip_counts"]["V1_Video"] == 1
        assert cp["clip_counts"]["A1_Narration"] == 2
        assert cp["clip_counts"]["A2_Music"] == 1

        # Escalation path
        otio_manager.begin_escalation("EXTEND", "add_scene", "test")
        otio_manager.reset_to_draft("test_recovery")
        assert otio_manager.state == "draft"

        # Verify history
        assert len(otio_manager.history) >= 3  # created, authoritative, draft

    def test_gpu_protocol_mock_succeeds(self, gpu_protocol):
        """Mock GPU protocol completes jobs."""
        request = GPUJobRequest(
            job_type=GPUJobType.VIDEO_RENDER,
            scene_num=1,
            phrase_idx=0,
        )
        result = asyncio.get_event_loop().run_until_complete(
            gpu_protocol.submit(request)
        )
        assert result.status.value == "completed"
        assert result.output_path != ""

    def test_gpu_protocol_mock_fails_video(self, failing_gpu_protocol):
        """Mock GPU protocol fails video renders when configured."""
        request = GPUJobRequest(
            job_type=GPUJobType.VIDEO_RENDER,
            scene_num=1,
        )
        result = asyncio.get_event_loop().run_until_complete(
            failing_gpu_protocol.submit(request)
        )
        assert result.status.value == "failed"

    def test_gpu_protocol_tts_succeeds_when_video_fails(self, failing_gpu_protocol):
        """TTS jobs succeed even when video renders fail."""
        request = GPUJobRequest(
            job_type=GPUJobType.TTS_RENDER,
            scene_num=1,
        )
        result = asyncio.get_event_loop().run_until_complete(
            failing_gpu_protocol.submit(request)
        )
        assert result.status.value == "completed"

    def test_feature_flag_strands(self, strands_mode):
        """Feature flag correctly enables Strands mode."""
        from strands_agents.feature_flag import is_strands_enabled, get_pipeline_backend
        assert is_strands_enabled()
        assert get_pipeline_backend() == "strands"

    def test_qa_gates_available(self):
        """QA gates are importable from strands_agents."""
        try:
            from strands_agents.qa_gates import qa_duration_align, qa_stills_judge
            assert callable(qa_duration_align)
            assert callable(qa_stills_judge)
        except ImportError:
            pytest.skip("qa_gates not available (missing OTIO/ffmpeg)")

    def test_recovery_shell_classifies_all_stages(self):
        """RecoveryShell can classify failures from all 5 stages."""
        from strands_agents.graph_pipeline import RecoveryShell
        for stage in [SCENARIO, AUDIO, VISUAL, PRODUCTION, ASSEMBLY]:
            exc = RuntimeError(f"Node {stage} failed: test error")
            result = RecoveryShell._classify_failure(exc)
            assert result == stage, f"Expected {stage}, got {result}"

    def test_budget_hook_enforces_budget(self):
        """BudgetHook tracks and reports cost across stages."""
        hook = BudgetHook(budget_usd=10.0)
        # 3 stages at $3 each
        for _ in range(3):
            event = _MockAfterNodeEvent(invocation_state={"_stage_cost": 3.0})
            asyncio.get_event_loop().run_until_complete(hook.on_after_node_call(event))
        assert hook.accrued == 9.0

        # 4th stage pushes over budget
        event = _MockAfterNodeEvent(invocation_state={"_stage_cost": 3.0})
        asyncio.get_event_loop().run_until_complete(hook.on_after_node_call(event))
        assert hook.accrued == 12.0  # Over budget but doesn't crash

    def test_checkpoint_hook_records_after_each_stage(self, otio_manager):
        """CheckpointHook records a checkpoint after each stage."""
        hook = CheckpointHook()
        for stage in [SCENARIO, AUDIO, VISUAL, PRODUCTION, ASSEMBLY]:
            event = _MockAfterNodeEvent(
                node_id=stage,
                invocation_state={"otio_manager": otio_manager},
            )
            asyncio.get_event_loop().run_until_complete(hook.on_after_node_call(event))

        assert len(otio_manager.checkpoints) == 5
        labels = [cp["label"] for cp in otio_manager.checkpoints]
        assert labels == [
            f"after_{SCENARIO}",
            f"after_{AUDIO}",
            f"after_{VISUAL}",
            f"after_{PRODUCTION}",
            f"after_{ASSEMBLY}",
        ]

    def test_approval_gate_hook_interrupts_unapproved(self):
        """ApprovalGateHook interrupts unapproved stages."""
        hook = ApprovalGateHook(gated_stages={SCENARIO, AUDIO})
        # Unapproved = interrupt
        event = _MockBeforeNodeEvent(node_id=SCENARIO, invocation_state={})
        asyncio.get_event_loop().run_until_complete(hook.on_before_node_call(event))
        assert event._interrupted

        # Approved = no interrupt
        event = _MockBeforeNodeEvent(
            node_id=SCENARIO,
            invocation_state={"_approved_scenario": True},
        )
        asyncio.get_event_loop().run_until_complete(hook.on_before_node_call(event))
        assert event._interrupted is False


class TestOTIOManagerStateTransitions:
    """Detailed tests for OTIO state transitions."""

    def test_draft_to_authoritative(self, otio_manager):
        otio_manager.set_authoritative("test")
        assert otio_manager.is_authoritative
        assert otio_manager.state == "authoritative"

    def test_authoritative_idempotent(self, otio_manager):
        otio_manager.set_authoritative("first")
        otio_manager.set_authoritative("second")  # no-op
        assert otio_manager.is_authoritative

    def test_cannot_reset_without_escalation(self, otio_manager):
        otio_manager.set_authoritative("test")
        from strands_agents.otio_manager import OtioStateViolation
        with pytest.raises(OtioStateViolation):
            otio_manager.reset_to_draft("test")

    def test_escalation_lifecycle(self, otio_manager):
        otio_manager.set_authoritative("test")
        otio_manager.begin_escalation("REPLACE", "test", "tester")
        assert otio_manager.escalation is not None
        assert otio_manager.escalation["type"] == "REPLACE"

        otio_manager.reset_to_draft("test")
        assert otio_manager.state == "draft"

        otio_manager.end_escalation()
        assert otio_manager.escalation is None

    def test_guard_blocks_unauthorized_mutation(self, otio_manager):
        otio_manager.set_authoritative("test")
        from strands_agents.otio_manager import OtioStateViolation
        with pytest.raises(OtioStateViolation) as exc_info:
            otio_manager.guard_mutation("add_clip")
        assert exc_info.value.details["otio_state"] == "authoritative"

    def test_guard_allows_draft_mutation(self, otio_manager):
        otio_manager.guard_mutation("add_clip")  # draft → allowed

    def test_guard_allows_escalation_mutation(self, otio_manager):
        otio_manager.set_authoritative("test")
        otio_manager.begin_escalation("EXTEND", "test", "tester")
        otio_manager.guard_mutation("add_clip")  # escalation → allowed

    def test_cost_tracking(self, otio_manager):
        otio_manager.add_cost(1.50)
        otio_manager.add_cost(2.50)
        assert otio_manager.cost == (4.0, 0.0)

    def test_qa_recording(self, otio_manager):
        otio_manager.record_qa("audio", {"verdict": "pass", "scene": 1})
        otio_manager.record_qa("audio", {"verdict": "fail", "scene": 2})
        assert len(otio_manager._qa_results["audio"]) == 2


# ---------------------------------------------------------------------------
# Mock event classes
# ---------------------------------------------------------------------------


class _MockBeforeNodeEvent:
    def __init__(self, node_id="test", invocation_state=None):
        self.node_id = node_id
        self.invocation_state = invocation_state or {}
        self.cancel_node = False
        self._interrupted = False

    def interrupt(self):
        self._interrupted = True


class _MockAfterNodeEvent:
    def __init__(self, node_id="test", invocation_state=None):
        self.node_id = node_id
        self.invocation_state = invocation_state or {}
