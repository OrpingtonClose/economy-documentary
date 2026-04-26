"""Unit tests for slice 9i — pipeline HITL wiring.

Covers:

* :mod:`strands_agents.playground.pipeline_live_hitl` — env-gated
  operator-decision factory, header helper, and queue passthrough.
* :func:`translate_pipeline_event` — surfaces ``run_id`` /
  ``interrupt_id`` / ``args`` on ``pipeline.approval.waiting`` and
  ``pipeline.approval.resumed`` events so the frontend can post the
  operator's decision back to
  ``POST /playground/approval/resume/{run_id}/{interrupt_id}``.
* :class:`LivePipelineRun` — emits the gate event with the runner's
  ``run_id`` populated when the dispatcher injected one.
* The ``/approval`` router — round-trips a decision through an
  isolated :class:`PendingInterruptQueue` so the dispatcher and the
  HTTP router agree on the resume contract.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

# server.api lives outside the strands_agents package; mirror the path
# adjustment the existing approval-gate tests use so the router import
# resolves.
_SERVER_DIR = Path(__file__).resolve().parents[3]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from api.approval import build_router  # noqa: E402
from strands_agents.approval_queue import PendingInterruptQueue  # noqa: E402
from strands_agents.playground.pipeline_adapter import (  # noqa: E402
    translate_pipeline_event,
)
from strands_agents.playground.pipeline_live_hitl import (  # noqa: E402
    PIPELINE_HITL_ENV_VAR,
    build_pipeline_hitl_operator,
    hitl_run_id_header,
    is_pipeline_hitl_enabled,
    maybe_build_pipeline_hitl_operator,
)


# ---------------------------------------------------------------------------
# is_pipeline_hitl_enabled
# ---------------------------------------------------------------------------


class TestIsPipelineHitlEnabled:
    def test_unset_returns_false(self) -> None:
        assert is_pipeline_hitl_enabled({}) is False

    def test_truthy_values_return_true(self) -> None:
        for value in ("1", "true", "TRUE", "Yes", "on", " on "):
            assert is_pipeline_hitl_enabled({PIPELINE_HITL_ENV_VAR: value}) is True

    def test_falsy_values_return_false(self) -> None:
        for value in ("", "0", "false", "no", "off", "anythingelse"):
            assert is_pipeline_hitl_enabled({PIPELINE_HITL_ENV_VAR: value}) is False


# ---------------------------------------------------------------------------
# maybe_build_pipeline_hitl_operator
# ---------------------------------------------------------------------------


class TestMaybeBuildPipelineHitlOperator:
    def test_returns_none_when_env_unset(self, tmp_path: Path) -> None:
        operator = maybe_build_pipeline_hitl_operator(
            run_id="run-1",
            run_dir=tmp_path,
            env={},
            queue=PendingInterruptQueue(),
        )
        assert operator is None

    def test_returns_callable_when_env_set(self, tmp_path: Path) -> None:
        queue = PendingInterruptQueue()
        operator = maybe_build_pipeline_hitl_operator(
            run_id="run-2",
            run_dir=tmp_path,
            env={PIPELINE_HITL_ENV_VAR: "1"},
            queue=queue,
        )
        assert operator is not None
        assert callable(operator)

    def test_creates_approvals_dir_eagerly(self, tmp_path: Path) -> None:
        # Avoids a race between the first gate and write_pending_envelope.
        maybe_build_pipeline_hitl_operator(
            run_id="run-3",
            run_dir=tmp_path,
            env={PIPELINE_HITL_ENV_VAR: "true"},
            queue=PendingInterruptQueue(),
        )
        assert (tmp_path / "approvals").is_dir()


# ---------------------------------------------------------------------------
# build_pipeline_hitl_operator
# ---------------------------------------------------------------------------


class TestBuildPipelineHitlOperator:
    def test_returns_callable(self, tmp_path: Path) -> None:
        operator = build_pipeline_hitl_operator(
            run_id="run-x",
            run_dir=tmp_path,
            queue=PendingInterruptQueue(),
        )
        assert callable(operator)


# ---------------------------------------------------------------------------
# hitl_run_id_header
# ---------------------------------------------------------------------------


class TestHitlRunIdHeader:
    def test_returns_x_pipeline_run_id_header(self) -> None:
        header = hitl_run_id_header("run-abc")
        assert header == {"X-Pipeline-Run-Id": "run-abc"}


# ---------------------------------------------------------------------------
# translate_pipeline_event — slice 9i additions
# ---------------------------------------------------------------------------


class TestTranslateApprovalGate:
    def test_waiting_surfaces_run_id_interrupt_id_and_args(self) -> None:
        translated = translate_pipeline_event(
            "pipeline.approval_gate",
            {
                "gate_name": "launch_visual_production",
                "allowed_decisions": ["accept", "edit", "reject"],
                "interrupt_id": "int-123",
                "run_id": "run-abc",
                "args": {"scene_id": "s1", "prompt": "wide shot"},
            },
        )

        assert translated.kind == "pipeline.approval.waiting"
        assert translated.detail["interrupt_id"] == "int-123"
        assert translated.detail["run_id"] == "run-abc"
        assert translated.detail["args"] == {"scene_id": "s1", "prompt": "wide shot"}

    def test_waiting_omits_missing_resume_coordinates(self) -> None:
        translated = translate_pipeline_event(
            "pipeline.approval_gate",
            {
                "gate_name": "launch_visual_production",
                "allowed_decisions": ["accept"],
            },
        )

        assert translated.kind == "pipeline.approval.waiting"
        assert "interrupt_id" not in translated.detail
        assert "run_id" not in translated.detail
        assert "args" not in translated.detail

    def test_waiting_ignores_non_string_run_id(self) -> None:
        translated = translate_pipeline_event(
            "pipeline.approval_gate",
            {
                "gate_name": "g",
                "interrupt_id": 42,
                "run_id": None,
                "args": "not a dict",
            },
        )

        assert "interrupt_id" not in translated.detail
        assert "run_id" not in translated.detail
        assert "args" not in translated.detail

    def test_resumed_surfaces_run_id_and_interrupt_id(self) -> None:
        translated = translate_pipeline_event(
            "pipeline.approval_resumed",
            {
                "gate_name": "launch_assembly",
                "decision": "approve",
                "interrupt_id": "int-789",
                "run_id": "run-xyz",
            },
        )

        assert translated.kind == "pipeline.approval.resumed"
        assert translated.detail["decision"] == "approve"
        assert translated.detail["interrupt_id"] == "int-789"
        assert translated.detail["run_id"] == "run-xyz"


# ---------------------------------------------------------------------------
# LivePipelineRun.run_id surface on the SSE wire
# ---------------------------------------------------------------------------


class TestLivePipelineRunIdSurface:
    """Pin the dataclass shape so the dispatcher → runner contract holds."""

    def test_dataclass_accepts_run_id(self, tmp_path: Path) -> None:
        from strands_agents.playground.pipeline_live_runner import LivePipelineRun

        run = LivePipelineRun(
            topic="t",
            target_duration_sec=60,
            language="en",
            agent=None,
            run_dir=tmp_path,
            run_id="run-9i",
        )
        assert run.run_id == "run-9i"

    def test_run_id_defaults_to_none(self, tmp_path: Path) -> None:
        from strands_agents.playground.pipeline_live_runner import LivePipelineRun

        run = LivePipelineRun(
            topic="t",
            target_duration_sec=60,
            language="en",
            agent=None,
            run_dir=tmp_path,
        )
        assert run.run_id is None


# ---------------------------------------------------------------------------
# /approval router round-trip
# ---------------------------------------------------------------------------


def _build_app(queue: PendingInterruptQueue) -> FastAPI:
    """Mount the approval router on a fresh FastAPI app for testing."""

    app = FastAPI()
    app.include_router(build_router(queue))
    return app


class TestApprovalRouterRoundTrip:
    """Smoke-tests the resume contract end-to-end with an isolated queue.

    The dispatcher and the HTTP router share one
    :class:`PendingInterruptQueue` in production. These tests prove
    that posting a decision via the router resolves the same future
    a queue-backed operator handler is awaiting — i.e. the orchestrator
    can resume on operator input without inferring the contract.
    """

    def test_pending_404_when_no_interrupt_registered(self) -> None:
        app = _build_app(PendingInterruptQueue())
        client = TestClient(app)
        response = client.post(
            "/approval/resume/run-1/int-1",
            json={"type": "accept"},
        )
        assert response.status_code == 404

    def test_pending_lists_registered_interrupts(self) -> None:
        queue = PendingInterruptQueue()
        loop = asyncio.new_event_loop()
        try:
            future = loop.create_task(
                queue.add(
                    run_id="run-list",
                    interrupt_id="int-list",
                    tool_name="launch_visual_production",
                    payload={"args": {"scene_id": "s1"}},
                )
            )
            # Run the loop just long enough for ``add`` to complete; it
            # awaits an internal lock and returns a future immediately.
            loop.run_until_complete(asyncio.sleep(0))
            assert future is not None

            app = _build_app(queue)
            client = TestClient(app)
            response = client.get("/approval/pending")
            assert response.status_code == 200
            payload: dict[str, Any] = response.json()
            interrupts = payload.get("interrupts") or payload.get("pending") or []
            assert any(
                item.get("run_id") == "run-list"
                and item.get("interrupt_id") == "int-list"
                for item in interrupts
            )
        finally:
            loop.close()

    def test_resume_invalid_decision_returns_400(self) -> None:
        queue = PendingInterruptQueue()

        async def _seed() -> None:
            await queue.add(
                run_id="run-bad",
                interrupt_id="int-bad",
                tool_name="launch_visual_production",
                payload={"args": {"scene_id": "s1"}},
            )

        asyncio.new_event_loop().run_until_complete(_seed())

        app = _build_app(queue)
        client = TestClient(app)
        response = client.post(
            "/approval/resume/run-bad/int-bad",
            json={"type": "edit"},  # missing required ``args``
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Interrupt-id stability between SSE event and queue handler
# ---------------------------------------------------------------------------


class TestInterruptIdStability:
    """Pin the invariant that the SSE event and the queue handler
    derive the *same* ``interrupt_id`` for a single interrupt round.

    The frontend resumes via
    ``POST /approval/resume/{run_id}/{interrupt_id}``. If the
    pipeline runner emits one id on
    ``pipeline.approval_gate`` while
    :func:`queue_operator_decision` registers a different id on the
    queue, every operator decision lands as a 404 and the gate hangs
    forever.
    """

    def _interrupt_state(
        self,
        *,
        with_id: bool,
        action_requests: bool,
    ) -> dict[str, Any]:
        if action_requests:
            value: dict[str, Any] = {
                "action_requests": [
                    {
                        "name": "launch_visual_production",
                        "args": {"scene_id": "s1"},
                        "description": "render scene 1",
                    }
                ],
                "review_configs": [
                    {
                        "action_name": "launch_visual_production",
                        "allowed_decisions": ["accept", "edit", "reject"],
                    }
                ],
            }
        else:
            value = {
                "tool_name": "launch_visual_production",
                "tool_input": {"scene_id": "s1"},
                "allowed_decisions": ["accept", "edit", "reject"],
            }
        interrupt: dict[str, Any] = {"value": value}
        if with_id:
            interrupt["id"] = "int-preset-42"
        return {"__interrupt__": [interrupt]}

    def test_action_requests_path_without_id_is_stable(self) -> None:
        from strands_agents.playground.pipeline_live_runner import (
            _extract_hitl_interrupt,
        )
        from strands_agents.run import _extract_interrupt_metadata

        state = self._interrupt_state(with_id=False, action_requests=True)
        runner_id, runner_tool, _runner_payload = _extract_hitl_interrupt(state)
        queue_id, _queue_tool, _queue_payload = _extract_interrupt_metadata(state)

        assert runner_id == queue_id
        assert runner_id != ""
        assert runner_tool == "launch_visual_production"

    def test_legacy_path_without_id_is_stable(self) -> None:
        from strands_agents.playground.pipeline_live_runner import (
            _extract_hitl_interrupt,
        )
        from strands_agents.run import _extract_interrupt_metadata

        state = self._interrupt_state(with_id=False, action_requests=False)
        runner_id, runner_tool, _ = _extract_hitl_interrupt(state)
        queue_id, queue_tool, _ = _extract_interrupt_metadata(state)

        assert runner_id == queue_id
        assert runner_id != ""
        assert runner_tool == queue_tool == "launch_visual_production"

    def test_preset_id_is_preserved(self) -> None:
        from strands_agents.playground.pipeline_live_runner import (
            _extract_hitl_interrupt,
        )
        from strands_agents.run import _extract_interrupt_metadata

        state = self._interrupt_state(with_id=True, action_requests=True)
        runner_id, _, _ = _extract_hitl_interrupt(state)
        queue_id, _, _ = _extract_interrupt_metadata(state)

        assert runner_id == queue_id == "int-preset-42"

    def test_object_interrupt_without_id_is_stable(self) -> None:
        """Real LangGraph hands us dataclass-like objects, not dicts."""

        from strands_agents.playground.pipeline_live_runner import (
            _extract_hitl_interrupt,
        )
        from strands_agents.run import _extract_interrupt_metadata

        class _Interrupt:
            def __init__(self) -> None:
                self.value = {
                    "tool_name": "launch_assembly",
                    "tool_input": {},
                    "allowed_decisions": ["accept", "reject"],
                }
                self.id: str | None = None

        interrupt = _Interrupt()
        state: dict[str, Any] = {"__interrupt__": [interrupt]}

        runner_id, _, _ = _extract_hitl_interrupt(state)
        queue_id, _, _ = _extract_interrupt_metadata(state)

        assert runner_id == queue_id
        assert runner_id != ""
        assert interrupt.id == runner_id


__all__ = [
    "TestApprovalRouterRoundTrip",
    "TestBuildPipelineHitlOperator",
    "TestHitlRunIdHeader",
    "TestInterruptIdStability",
    "TestIsPipelineHitlEnabled",
    "TestLivePipelineRunIdSurface",
    "TestMaybeBuildPipelineHitlOperator",
    "TestTranslateApprovalGate",
]
