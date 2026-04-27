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
                "decision": "accept",
                "interrupt_id": "int-789",
                "run_id": "run-xyz",
            },
        )

        assert translated.kind == "pipeline.approval.resumed"
        assert translated.detail["decision"] == "accept"
        assert translated.detail["interrupt_id"] == "int-789"
        assert translated.detail["run_id"] == "run-xyz"


# ---------------------------------------------------------------------------
# _decision_from_command — project-vocab on the SSE wire
# ---------------------------------------------------------------------------


class TestDecisionFromCommand:
    """Pin that the SSE ``pipeline.approval.resumed`` event echoes the
    operator's original project vocab (``accept`` / ``edit`` / ``reject``
    / ``respond``), not the langchain-translated form (``approve`` /
    ``edit`` / ``reject``).

    The bug this guards against: ``langchain_resume_command_from_decision``
    builds a ``Command(resume={"decisions": [{"type": "approve"}, ...]})``
    where the inner type is langchain vocab. Reading that directly into
    the SSE event leaks ``approve`` to the frontend, so the operator
    sees ``resumed: approve`` after clicking ``accept``. The
    ``_project_decision_type`` sidecar fixes this by carrying the
    operator's original click through the resume payload.
    """

    def test_sidecar_returns_project_vocab_accept(self) -> None:
        from langgraph.types import Command

        from strands_agents.playground.pipeline_live_runner import (
            _decision_from_command,
        )

        command = Command(
            resume={
                "decisions": [{"type": "approve"}],
                "_project_decision_type": "accept",
            },
        )
        assert _decision_from_command(command) == "accept"

    def test_sidecar_returns_project_vocab_respond(self) -> None:
        from langgraph.types import Command

        from strands_agents.playground.pipeline_live_runner import (
            _decision_from_command,
        )

        # ``respond`` collapses to ``reject`` on the langchain side but
        # the sidecar preserves the operator's actual click.
        command = Command(
            resume={
                "decisions": [
                    {"type": "reject", "message": "see attached"},
                ],
                "_project_decision_type": "respond",
            },
        )
        assert _decision_from_command(command) == "respond"

    def test_sidecar_returns_project_vocab_edit_and_reject(self) -> None:
        from langgraph.types import Command

        from strands_agents.playground.pipeline_live_runner import (
            _decision_from_command,
        )

        edit_cmd = Command(
            resume={
                "decisions": [
                    {
                        "type": "edit",
                        "edited_action": {"name": "t", "args": {}},
                    },
                ],
                "_project_decision_type": "edit",
            },
        )
        reject_cmd = Command(
            resume={
                "decisions": [{"type": "reject", "message": "x"}],
                "_project_decision_type": "reject",
            },
        )
        assert _decision_from_command(edit_cmd) == "edit"
        assert _decision_from_command(reject_cmd) == "reject"

    def test_reverse_maps_langchain_vocab_when_sidecar_missing(self) -> None:
        # Legacy callers that build ``Command(resume={"decisions": ...})``
        # without the sidecar still land on project vocab via the reverse
        # map. ``approve`` → ``accept``, ``reject`` → ``reject``,
        # ``edit`` → ``edit``.
        from langgraph.types import Command

        from strands_agents.playground.pipeline_live_runner import (
            _decision_from_command,
        )

        approve_cmd = Command(resume={"decisions": [{"type": "approve"}]})
        reject_cmd = Command(resume={"decisions": [{"type": "reject"}]})
        edit_cmd = Command(resume={"decisions": [{"type": "edit"}]})
        assert _decision_from_command(approve_cmd) == "accept"
        assert _decision_from_command(reject_cmd) == "reject"
        assert _decision_from_command(edit_cmd) == "edit"

    def test_legacy_single_decision_shape_passthrough(self) -> None:
        from langgraph.types import Command

        from strands_agents.playground.pipeline_live_runner import (
            _decision_from_command,
        )

        legacy = Command(resume={"type": "accept"})
        assert _decision_from_command(legacy) == "accept"

    def test_no_resume_falls_back_to_respond(self) -> None:
        from langgraph.types import Command

        from strands_agents.playground.pipeline_live_runner import (
            _decision_from_command,
        )

        empty = Command()
        assert _decision_from_command(empty) == "respond"

    def test_auto_accept_command_round_trips_to_accept(self) -> None:
        # ``auto_accept_interrupt`` is the CI/demo default. After this
        # fix it must emit a Command whose ``_decision_from_command``
        # readout is project-vocab ``accept`` so the SSE event matches
        # what the user clicked.
        from strands_agents.playground.pipeline_live_runner import (
            _decision_from_command,
            auto_accept_interrupt,
        )

        command = asyncio.run(
            auto_accept_interrupt({"__interrupt__": [{"value": "x"}]}),
        )
        assert _decision_from_command(command) == "accept"


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

    def test_frozen_interrupt_without_id_is_stable_via_state_cache(
        self,
    ) -> None:
        """Frozen dataclass / NamedTuple variants reject ``setattr``.

        The id-stability invariant must hold anyway, since the SSE
        event and the queue handler resolve from the same ``state``
        dict. The fallback is the per-state cache.
        """

        from dataclasses import dataclass

        from strands_agents.playground.pipeline_live_runner import (
            _extract_hitl_interrupt,
        )
        from strands_agents.run import (
            _INTERRUPT_ID_CACHE_KEY,
            _extract_interrupt_metadata,
        )

        @dataclass(frozen=True)
        class _FrozenInterrupt:
            value: dict[str, Any]

        interrupt = _FrozenInterrupt(
            value={
                "tool_name": "launch_visual_production",
                "tool_input": {"scene_id": "s1"},
                "allowed_decisions": ["accept", "edit", "reject"],
            }
        )
        state: dict[str, Any] = {"__interrupt__": [interrupt]}

        runner_id, _, _ = _extract_hitl_interrupt(state)
        queue_id, _, _ = _extract_interrupt_metadata(state)

        assert runner_id == queue_id
        assert runner_id != ""
        # State cache is the canonical fallback when setattr fails.
        cache = state[_INTERRUPT_ID_CACHE_KEY]
        assert cache[id(interrupt)] == runner_id

        # And a third extraction round resolves to the same id, proving
        # the cache is what holds the invariant (not the object).
        third_id, _, _ = _extract_interrupt_metadata(state)
        assert third_id == runner_id


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
