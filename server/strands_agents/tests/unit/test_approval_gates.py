"""Unit tests for the approval-gate surface (component 15).

Covers:

* :mod:`strands_agents.approval` — decision validation, resume-command
  construction, audit-trail persistence, :data:`INTERRUPT_GATE_CONFIG`
  per-gate decision allowlists.
* :mod:`strands_agents.approval_queue` — :class:`PendingInterruptQueue`
  add/resolve/cancel/list semantics and the process-wide singleton.
* :mod:`server.api.approval` — the FastAPI router's two endpoints
  (``GET /approval/pending`` + ``POST /approval/resume/{run_id}/{id}``)
  including 404 / 400 error paths.
* :mod:`strands_agents.run` — resume handlers
  (:func:`queue_operator_decision`, :func:`replay_operator_decisions`)
  produce :class:`Command` objects and persist audit records.
* :mod:`strands_agents.evals.experiments.approval` — the 6-case
  experiment scores 1.0 end-to-end with pre-captured trajectories.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.types import Command

# server.api imports from top-level ``strands_agents`` (no leading dot);
# emulate the package path so the router picks up the right module.
_SERVER_DIR = Path(__file__).resolve().parents[3]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from api.approval import build_router  # noqa: E402
from strands_agents.approval import (  # noqa: E402
    INTERRUPT_GATE_CONFIG,
    ApprovalDecision,
    ApprovalRecord,
    langchain_resume_command_from_decision,
    new_interrupt_id,
    request_human_approval,
    resume_command_from_decision,
    validate_decision,
    write_approval_record,
    write_pending_envelope,
)
from strands_agents.approval_queue import (  # noqa: E402
    PendingInterrupt,
    PendingInterruptQueue,
    get_default_queue,
    reset_default_queue,
)
from strands_agents.evals.experiments.approval import (  # noqa: E402
    approval_task,
    build_approval_experiment,
)
from strands_agents.run import (  # noqa: E402
    _auto_reject_interrupt,
    _extract_interrupt_metadata,
    queue_operator_decision,
    replay_operator_decisions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _interrupt_state(
    tool_name: str,
    *,
    interrupt_id: str | None = None,
    args: dict[str, Any] | None = None,
    allowed: list[str] | None = None,
) -> dict[str, Any]:
    """Build a fake graph state dict resembling LangGraph's ``__interrupt__``."""

    return {
        "__interrupt__": [
            {
                "id": interrupt_id or "int-test-0001",
                "value": {
                    "tool_name": tool_name,
                    "tool_input": args or {"scene_id": "s1"},
                    "description": "test gate",
                    "allowed_decisions": allowed
                    or INTERRUPT_GATE_CONFIG[tool_name]["allowed_decisions"],
                },
            },
        ],
    }


# ---------------------------------------------------------------------------
# INTERRUPT_GATE_CONFIG + validate_decision
# ---------------------------------------------------------------------------


class TestInterruptGateConfig:
    def test_visual_allows_all_four(self) -> None:
        allowed = INTERRUPT_GATE_CONFIG["launch_visual_production"]["allowed_decisions"]
        assert set(allowed) == {"accept", "edit", "reject", "respond"}

    def test_assembly_drops_edit(self) -> None:
        allowed = INTERRUPT_GATE_CONFIG["launch_assembly"]["allowed_decisions"]
        assert "edit" not in allowed
        assert set(allowed) == {"accept", "reject", "respond"}

    def test_human_approval_accept_or_respond_only(self) -> None:
        allowed = INTERRUPT_GATE_CONFIG["request_human_approval"]["allowed_decisions"]
        assert set(allowed) == {"accept", "respond"}


class TestValidateDecision:
    def test_accept_passes(self) -> None:
        validate_decision("launch_visual_production", {"type": "accept"})

    def test_edit_requires_args_dict(self) -> None:
        with pytest.raises(ValueError, match="edit decision requires"):
            validate_decision("launch_visual_production", {"type": "edit"})

    def test_edit_on_assembly_rejected(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            validate_decision("launch_assembly", {"type": "edit", "args": {"x": 1}})

    def test_reject_requires_non_empty_reason(self) -> None:
        with pytest.raises(ValueError, match="non-empty 'reason'"):
            validate_decision(
                "launch_visual_production", {"type": "reject", "reason": "  "}
            )

    def test_respond_requires_content_key(self) -> None:
        with pytest.raises(ValueError, match="requires 'content'"):
            validate_decision("launch_assembly", {"type": "respond"})

    def test_respond_accepts_dict_content(self) -> None:
        validate_decision(
            "launch_assembly",
            {"type": "respond", "content": {"action": "hold"}},
        )

    def test_respond_accepts_string_content(self) -> None:
        validate_decision(
            "launch_assembly",
            {"type": "respond", "content": "hold"},
        )

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown decision type"):
            validate_decision("launch_visual_production", {"type": "postpone"})

    def test_unknown_tool_defaults_to_full_superset(self) -> None:
        # Unknown tools default to accept/edit/reject/respond so the
        # queue-side validator matches the graph's interrupt_on config
        # (pipeline._build_interrupt_on advertises the same superset).
        validate_decision("custom_tool", {"type": "accept"})
        validate_decision("custom_tool", {"type": "edit", "args": {"x": 1}})


# ---------------------------------------------------------------------------
# resume_command_from_decision
# ---------------------------------------------------------------------------


class TestResumeCommand:
    def test_returns_langgraph_command(self) -> None:
        command = resume_command_from_decision(
            "launch_visual_production", {"type": "accept"}
        )
        assert isinstance(command, Command)
        assert command.resume == {"type": "accept"}

    def test_edit_payload_propagates_args(self) -> None:
        command = resume_command_from_decision(
            "launch_visual_production",
            {"type": "edit", "args": {"scene_id": "s1", "prompt": "new"}},
        )
        assert command.resume["type"] == "edit"
        assert command.resume["args"] == {"scene_id": "s1", "prompt": "new"}

    def test_invalid_decision_raises_before_command(self) -> None:
        with pytest.raises(ValueError):
            resume_command_from_decision(
                "launch_assembly", {"type": "edit", "args": {}}
            )


# ---------------------------------------------------------------------------
# langchain_resume_command_from_decision
# ---------------------------------------------------------------------------


class TestLangchainResumeCommand:
    """The shape ``langchain.agents.middleware.HumanInTheLoopMiddleware``
    requires when resuming a paused graph.

    Producing the legacy single-decision shape (``{"type": "approve"}``)
    raises ``KeyError: 'decisions'`` mid-run when the middleware tries
    to unwrap ``response["decisions"]``. These tests pin the contract.
    """

    def test_accept_maps_to_approve_in_decisions_array(self) -> None:
        command = langchain_resume_command_from_decision(
            "launch_visual_production", {"type": "accept"}
        )
        assert isinstance(command, Command)
        assert command.resume == {
            "decisions": [{"type": "approve"}],
            "_project_decision_type": "accept",
        }

    def test_edit_wraps_args_under_edited_action(self) -> None:
        command = langchain_resume_command_from_decision(
            "launch_visual_production",
            {"type": "edit", "args": {"scene_id": "s1", "prompt": "x"}},
        )
        assert command.resume == {
            "decisions": [
                {
                    "type": "edit",
                    "edited_action": {
                        "name": "launch_visual_production",
                        "args": {"scene_id": "s1", "prompt": "x"},
                    },
                },
            ],
            "_project_decision_type": "edit",
        }

    def test_reject_carries_reason_as_message(self) -> None:
        command = langchain_resume_command_from_decision(
            "launch_visual_production",
            {"type": "reject", "reason": "bad shot"},
        )
        assert command.resume == {
            "decisions": [{"type": "reject", "message": "bad shot"}],
            "_project_decision_type": "reject",
        }

    def test_respond_folds_into_reject(self) -> None:
        # langchain HITL has no ``respond`` action; surface the
        # operator's content as a reject ``message`` so the agent
        # transcript still records it. The ``_project_decision_type``
        # sidecar still preserves the original ``respond`` so the SSE
        # event echoes the operator's actual click instead of the
        # langchain-translated ``reject``.
        command = langchain_resume_command_from_decision(
            "request_human_approval",
            {"type": "respond", "content": "see attached"},
        )
        assert command.resume == {
            "decisions": [{"type": "reject", "message": "see attached"}],
            "_project_decision_type": "respond",
        }

    def test_action_count_replicates_decision(self) -> None:
        # When N parallel ``action_requests`` share a single gate,
        # langchain validates ``len(decisions) == N``.
        command = langchain_resume_command_from_decision(
            "launch_visual_production",
            {"type": "accept"},
            action_count=5,
        )
        assert command.resume == {
            "decisions": [{"type": "approve"}] * 5,
            "_project_decision_type": "accept",
        }

    def test_action_count_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="action_count must be >= 1"):
            langchain_resume_command_from_decision(
                "launch_visual_production",
                {"type": "accept"},
                action_count=0,
            )

    def test_invalid_decision_raises_before_command(self) -> None:
        with pytest.raises(ValueError):
            langchain_resume_command_from_decision(
                "launch_assembly",
                {"type": "edit", "args": {}},
            )

    def test_decisions_are_independent_dicts(self) -> None:
        # Mutating one replicated decision must not affect siblings.
        command = langchain_resume_command_from_decision(
            "launch_visual_production",
            {"type": "accept"},
            action_count=3,
        )
        decisions = command.resume["decisions"]
        decisions[0]["type"] = "reject"
        assert decisions[1] == {"type": "approve"}
        assert decisions[2] == {"type": "approve"}


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


class TestAuditTrail:
    def test_write_pending_envelope_round_trip(self, tmp_path: Path) -> None:
        path = write_pending_envelope(
            tmp_path,
            interrupt_id="int-aaa",
            tool_name="launch_visual_production",
            payload={"args": {"scene_id": "s1"}},
        )
        assert path.exists()
        assert path.parent == tmp_path / "approvals"
        data = json.loads(path.read_text())
        assert data["interrupt_id"] == "int-aaa"
        assert data["tool_name"] == "launch_visual_production"
        assert "at" in data
        assert data["payload"] == {"args": {"scene_id": "s1"}}

    def test_write_approval_record_clears_pending(self, tmp_path: Path) -> None:
        write_pending_envelope(
            tmp_path,
            interrupt_id="int-bbb",
            tool_name="launch_assembly",
            payload={"args": {}},
        )
        pending = tmp_path / "approvals" / "pending_int-bbb.json"
        assert pending.exists()

        record = ApprovalRecord(
            interrupt_id="int-bbb",
            tool_name="launch_assembly",
            operator="op@example.com",
            decision={"type": "accept"},
        )
        resume_path = write_approval_record(tmp_path, record)
        assert resume_path.exists()
        assert resume_path.name == "resume_int-bbb.json"
        assert not pending.exists()
        data = json.loads(resume_path.read_text())
        assert data["operator"] == "op@example.com"
        assert data["decision"]["type"] == "accept"
        assert data["at"]

    def test_new_interrupt_id_is_unique(self) -> None:
        ids = {new_interrupt_id() for _ in range(10)}
        assert len(ids) == 10
        assert all(value.startswith("int-") for value in ids)


# ---------------------------------------------------------------------------
# request_human_approval @tool
# ---------------------------------------------------------------------------


class TestRequestHumanApproval:
    def test_returns_pending_envelope_outside_middleware(self) -> None:
        result = request_human_approval.invoke(
            {
                "reason": "escalation:skip_scene_s3",
                "summary": "GPU OOM on s3 x3; skip or abort?",
                "options": ["skip", "abort"],
                "context_paths": ["/runs/r1/errors.log"],
            },
        )
        assert result["status"] == "pending"
        assert result["reason"] == "escalation:skip_scene_s3"
        assert result["options"] == ["skip", "abort"]
        assert result["context_paths"] == ["/runs/r1/errors.log"]

    def test_defaults_empty_lists(self) -> None:
        result = request_human_approval.invoke(
            {"reason": "x", "summary": "y"},
        )
        assert result["options"] == []
        assert result["context_paths"] == []


# ---------------------------------------------------------------------------
# PendingInterruptQueue
# ---------------------------------------------------------------------------


class TestPendingInterruptQueue:
    def test_add_returns_pending_future(self) -> None:
        async def _main() -> None:
            queue = PendingInterruptQueue()
            future = await queue.add(
                run_id="r1",
                interrupt_id="int-1",
                tool_name="launch_visual_production",
                payload={},
            )
            assert not future.done()
            pending = queue.list_pending()
            assert len(pending) == 1
            assert pending[0].run_id == "r1"

        asyncio.run(_main())

    def test_resolve_completes_future(self) -> None:
        async def _main() -> None:
            queue = PendingInterruptQueue()
            future = await queue.add(
                run_id="r1",
                interrupt_id="int-1",
                tool_name="launch_visual_production",
                payload={},
            )
            await queue.resolve("r1", "int-1", {"type": "accept"})
            assert future.done()
            assert future.result() == {"type": "accept"}
            assert queue.list_pending() == []

        asyncio.run(_main())

    def test_resolve_missing_raises_keyerror(self) -> None:
        async def _main() -> None:
            queue = PendingInterruptQueue()
            with pytest.raises(KeyError):
                await queue.resolve("r1", "int-missing", {"type": "accept"})

        asyncio.run(_main())

    def test_resolve_invalid_decision_raises_valueerror(self) -> None:
        async def _main() -> None:
            queue = PendingInterruptQueue()
            await queue.add(
                run_id="r1",
                interrupt_id="int-1",
                tool_name="launch_assembly",
                payload={},
            )
            with pytest.raises(ValueError):
                # edit not allowed for launch_assembly
                await queue.resolve("r1", "int-1", {"type": "edit", "args": {"x": 1}})

        asyncio.run(_main())

    def test_add_duplicate_raises(self) -> None:
        async def _main() -> None:
            queue = PendingInterruptQueue()
            await queue.add(
                run_id="r1",
                interrupt_id="int-1",
                tool_name="launch_visual_production",
                payload={},
            )
            with pytest.raises(ValueError, match="already pending"):
                await queue.add(
                    run_id="r1",
                    interrupt_id="int-1",
                    tool_name="launch_visual_production",
                    payload={},
                )

        asyncio.run(_main())

    def test_cancel_fails_future(self) -> None:
        async def _main() -> None:
            queue = PendingInterruptQueue()
            future = await queue.add(
                run_id="r1",
                interrupt_id="int-1",
                tool_name="launch_visual_production",
                payload={},
            )
            await queue.cancel("r1", "int-1", reason="run aborted")
            assert future.done()
            with pytest.raises(RuntimeError, match="cancelled"):
                future.result()

        asyncio.run(_main())

    def test_list_pending_filters_by_run_id(self) -> None:
        async def _main() -> None:
            queue = PendingInterruptQueue()
            await queue.add(
                run_id="r1",
                interrupt_id="int-1",
                tool_name="launch_visual_production",
                payload={},
            )
            await queue.add(
                run_id="r2",
                interrupt_id="int-2",
                tool_name="launch_assembly",
                payload={},
            )
            assert len(queue.list_pending()) == 2
            assert len(queue.list_pending(run_id="r1")) == 1
            assert queue.list_pending(run_id="r1")[0].interrupt_id == "int-1"

        asyncio.run(_main())

    def test_pending_interrupt_to_dict_is_json_ready(self) -> None:
        pending = PendingInterrupt(
            run_id="r1",
            interrupt_id="int-1",
            tool_name="launch_assembly",
            payload={"args": {}},
        )
        data = pending.to_dict()
        json.dumps(data)  # must be serializable
        assert data["run_id"] == "r1"
        assert data["tool_name"] == "launch_assembly"

    def test_default_queue_is_singleton(self) -> None:
        reset_default_queue()
        queue_a = get_default_queue()
        queue_b = get_default_queue()
        assert queue_a is queue_b
        reset_default_queue()
        queue_c = get_default_queue()
        assert queue_c is not queue_a


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_client() -> tuple[TestClient, PendingInterruptQueue]:
    """TestClient bound to an isolated queue (no singleton spillover)."""

    queue = PendingInterruptQueue()
    app = FastAPI()
    app.include_router(build_router(queue))
    return TestClient(app), queue


class TestApprovalRouter:
    def test_pending_empty_returns_empty_list(
        self, api_client: tuple[TestClient, PendingInterruptQueue]
    ) -> None:
        client, _ = api_client
        response = client.get("/approval/pending")
        assert response.status_code == 200
        assert response.json() == {"pending": []}

    def test_pending_lists_registered_interrupts(
        self, api_client: tuple[TestClient, PendingInterruptQueue]
    ) -> None:
        client, queue = api_client

        async def _seed() -> None:
            await queue.add(
                run_id="r1",
                interrupt_id="int-1",
                tool_name="launch_visual_production",
                payload={"args": {"scene_id": "s1"}},
            )

        asyncio.run(_seed())
        response = client.get("/approval/pending")
        assert response.status_code == 200
        body = response.json()
        assert len(body["pending"]) == 1
        assert body["pending"][0]["run_id"] == "r1"
        assert body["pending"][0]["tool_name"] == "launch_visual_production"

    def test_pending_filters_by_run_id(
        self, api_client: tuple[TestClient, PendingInterruptQueue]
    ) -> None:
        client, queue = api_client

        async def _seed() -> None:
            await queue.add(
                run_id="r1",
                interrupt_id="int-1",
                tool_name="launch_visual_production",
                payload={},
            )
            await queue.add(
                run_id="r2",
                interrupt_id="int-2",
                tool_name="launch_assembly",
                payload={},
            )

        asyncio.run(_seed())
        response = client.get("/approval/pending", params={"run_id": "r2"})
        assert response.status_code == 200
        body = response.json()
        assert len(body["pending"]) == 1
        assert body["pending"][0]["run_id"] == "r2"

    def test_resume_success_resolves_queue(
        self, api_client: tuple[TestClient, PendingInterruptQueue]
    ) -> None:
        client, queue = api_client

        async def _seed() -> asyncio.Future[ApprovalDecision]:
            return await queue.add(
                run_id="r1",
                interrupt_id="int-1",
                tool_name="launch_visual_production",
                payload={},
            )

        future = asyncio.run(_seed())
        response = client.post(
            "/approval/resume/r1/int-1",
            json={"type": "accept"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "status": "resolved",
            "decision_type": "accept",
        }
        assert future.done()
        assert future.result() == {"type": "accept"}

    def test_resume_edit_propagates_args(
        self, api_client: tuple[TestClient, PendingInterruptQueue]
    ) -> None:
        client, queue = api_client

        async def _seed() -> asyncio.Future[ApprovalDecision]:
            return await queue.add(
                run_id="r1",
                interrupt_id="int-1",
                tool_name="launch_visual_production",
                payload={},
            )

        future = asyncio.run(_seed())
        response = client.post(
            "/approval/resume/r1/int-1",
            json={
                "type": "edit",
                "args": {"scene_id": "s1", "prompt": "tweaked"},
            },
        )
        assert response.status_code == 200
        assert future.result()["args"]["prompt"] == "tweaked"

    def test_resume_unknown_returns_404(
        self, api_client: tuple[TestClient, PendingInterruptQueue]
    ) -> None:
        client, _ = api_client
        response = client.post(
            "/approval/resume/r1/int-missing",
            json={"type": "accept"},
        )
        assert response.status_code == 404

    def test_resume_disallowed_decision_returns_400(
        self, api_client: tuple[TestClient, PendingInterruptQueue]
    ) -> None:
        client, queue = api_client

        async def _seed() -> None:
            await queue.add(
                run_id="r1",
                interrupt_id="int-1",
                tool_name="launch_assembly",
                payload={},
            )

        asyncio.run(_seed())
        response = client.post(
            "/approval/resume/r1/int-1",
            json={"type": "edit", "args": {"x": 1}},
        )
        assert response.status_code == 400
        assert "not allowed" in response.json()["detail"]

    def test_resume_missing_reason_returns_400(
        self, api_client: tuple[TestClient, PendingInterruptQueue]
    ) -> None:
        client, queue = api_client

        async def _seed() -> None:
            await queue.add(
                run_id="r1",
                interrupt_id="int-1",
                tool_name="launch_visual_production",
                payload={},
            )

        asyncio.run(_seed())
        response = client.post(
            "/approval/resume/r1/int-1",
            json={"type": "reject", "reason": ""},
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Resume handlers (run.py)
# ---------------------------------------------------------------------------


class TestExtractInterruptMetadata:
    def test_reads_dict_interrupt(self) -> None:
        state = _interrupt_state("launch_visual_production")
        interrupt_id, tool_name, payload = _extract_interrupt_metadata(state)
        assert interrupt_id == "int-test-0001"
        assert tool_name == "launch_visual_production"
        assert payload["args"] == {"scene_id": "s1"}
        assert payload["allowed_decisions"]

    def test_mints_id_when_missing(self) -> None:
        state = {
            "__interrupt__": [
                {"value": {"tool_name": "launch_assembly", "tool_input": {}}},
            ],
        }
        interrupt_id, tool_name, _ = _extract_interrupt_metadata(state)
        assert interrupt_id.startswith("int-")
        assert tool_name == "launch_assembly"

    def test_empty_state_raises(self) -> None:
        with pytest.raises(RuntimeError, match="no pending interrupt"):
            _extract_interrupt_metadata({"__interrupt__": []})


class TestAutoRejectInterrupt:
    def test_returns_reject_command(self) -> None:
        command = asyncio.run(
            _auto_reject_interrupt({"__interrupt__": ["x"]}),
        )
        assert isinstance(command, Command)
        # Auto-reject emits the langchain HITL middleware shape:
        # ``{"decisions": [...]}`` with one entry per action_request.
        # Tool name is unknown for a bare ``["x"]`` interrupt; the
        # default-allowed superset includes "reject", so the auto path
        # picks reject (mapped to langchain ``reject`` with ``message``).
        assert command.resume == {
            "decisions": [
                {"type": "reject", "message": "no operator attached"},
            ],
            "_project_decision_type": "reject",
        }

    def test_request_human_approval_gets_respond_not_reject(self) -> None:
        # request_human_approval only allows accept/respond.
        # Sending reject would bypass validate_decision and hit the
        # middleware with an invalid payload. Project ``respond`` has
        # no langchain HITL counterpart, so it folds into ``reject``
        # with the operator's ``content`` surfaced as ``message``.
        state = _interrupt_state("request_human_approval")
        command = asyncio.run(_auto_reject_interrupt(state))
        assert isinstance(command, Command)
        assert command.resume == {
            "decisions": [
                {"type": "reject", "message": "no operator attached"},
            ],
            "_project_decision_type": "respond",
        }

    def test_launch_visual_production_gets_reject(self) -> None:
        state = _interrupt_state("launch_visual_production")
        command = asyncio.run(_auto_reject_interrupt(state))
        decisions = command.resume["decisions"]
        assert len(decisions) == 1
        assert decisions[0]["type"] == "reject"

    def test_langchain_action_requests_replicate_decisions(self) -> None:
        # When the interrupt comes from langchain HumanInTheLoopMiddleware
        # with N parallel ``action_requests``, the resume command must
        # carry exactly N decisions or the middleware raises
        # ``ValueError: Number of human decisions ... does not match
        # number of hanging tool calls``.
        state = {
            "__interrupt__": [
                {
                    "id": "int-multi",
                    "value": {
                        "action_requests": [
                            {
                                "name": "launch_visual_production",
                                "args": {"scene_id": f"s{i}"},
                            }
                            for i in range(3)
                        ],
                        "review_configs": [
                            {
                                "action_name": "launch_visual_production",
                                "allowed_decisions": [
                                    "approve",
                                    "edit",
                                    "reject",
                                ],
                            }
                        ]
                        * 3,
                    },
                },
            ],
        }
        command = asyncio.run(_auto_reject_interrupt(state))
        decisions = command.resume["decisions"]
        assert len(decisions) == 3
        assert all(d["type"] == "reject" for d in decisions)


class TestQueueOperatorDecision:
    def test_full_round_trip_persists_audit(self, tmp_path: Path) -> None:
        async def _main() -> None:
            queue = PendingInterruptQueue()
            handler = queue_operator_decision(
                run_id="r1",
                run_dir=tmp_path,
                queue=queue,
                operator="op@example.com",
            )
            state = _interrupt_state(
                "launch_visual_production",
                interrupt_id="int-abc",
            )

            async def _resolve() -> None:
                # Yield once so the handler registers on the queue.
                for _ in range(50):
                    if queue.list_pending():
                        break
                    await asyncio.sleep(0)
                await queue.resolve("r1", "int-abc", {"type": "accept"})

            handler_task = asyncio.create_task(handler(state))
            await _resolve()
            command = await handler_task

            assert isinstance(command, Command)
            # Resumes through the langchain HITL middleware shape so a
            # legacy ``{"type": "accept"}`` does not crash on
            # ``KeyError: 'decisions'`` mid-run.
            assert command.resume == {
                "decisions": [{"type": "approve"}],
                "_project_decision_type": "accept",
            }
            resume_file = tmp_path / "approvals" / "resume_int-abc.json"
            assert resume_file.exists()
            record = json.loads(resume_file.read_text())
            assert record["operator"] == "op@example.com"
            assert record["decision"]["type"] == "accept"
            # Pending envelope should have been cleaned up.
            assert not (tmp_path / "approvals" / "pending_int-abc.json").exists()

        asyncio.run(_main())

    def test_defaults_to_global_queue(self, tmp_path: Path) -> None:
        reset_default_queue()
        handler = queue_operator_decision(run_id="r1", run_dir=tmp_path)
        # Calling the handler factory must not require a queue argument.
        assert callable(handler)
        reset_default_queue()


class TestReplayOperatorDecisions:
    def test_replays_in_order(self, tmp_path: Path) -> None:
        async def _main() -> None:
            decisions: list[ApprovalDecision] = [
                {"type": "accept"},
                {"type": "reject", "reason": "no"},
            ]
            handler = replay_operator_decisions(decisions, run_dir=tmp_path)
            first = await handler(
                _interrupt_state("launch_visual_production", interrupt_id="int-1"),
            )
            second = await handler(
                _interrupt_state("launch_visual_production", interrupt_id="int-2"),
            )
            assert first.resume == {
                "decisions": [{"type": "approve"}],
                "_project_decision_type": "accept",
            }
            assert second.resume == {
                "decisions": [{"type": "reject", "message": "no"}],
                "_project_decision_type": "reject",
            }
            assert (tmp_path / "approvals" / "resume_int-1.json").exists()
            assert (tmp_path / "approvals" / "resume_int-2.json").exists()

        asyncio.run(_main())

    def test_exhausted_list_raises(self) -> None:
        async def _main() -> None:
            handler = replay_operator_decisions([])
            with pytest.raises(RuntimeError, match="exhausted"):
                await handler(
                    _interrupt_state("launch_visual_production", interrupt_id="int-1"),
                )

        asyncio.run(_main())

    def test_invalid_decision_surfaces_validation_error(self, tmp_path: Path) -> None:
        async def _main() -> None:
            # edit not allowed for launch_assembly
            handler = replay_operator_decisions(
                [{"type": "edit", "args": {"x": 1}}],
                run_dir=tmp_path,
            )
            with pytest.raises(ValueError):
                await handler(
                    _interrupt_state("launch_assembly", interrupt_id="int-1"),
                )

        asyncio.run(_main())


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


class TestApprovalExperiment:
    def test_experiment_has_six_cases(self) -> None:
        experiment = build_approval_experiment()
        names = [case.name for case in experiment.cases]
        assert names == [
            "accept_visual_dispatch",
            "edit_visual_prompt",
            "reject_visual_dispatch",
            "respond_assembly_hold",
            "escalation_decision",
            "resume_after_restart",
        ]

    def test_experiment_has_three_evaluators(self) -> None:
        experiment = build_approval_experiment()
        assert len(experiment.evaluators) == 3

    def test_approval_task_wraps_full_trajectory(self) -> None:
        experiment = build_approval_experiment()
        case = next(c for c in experiment.cases if c.name == "edit_visual_prompt")
        task_output = approval_task(case)
        assert isinstance(task_output["trajectory"], list)
        # Trajectory must contain both an interrupt and a follow-up
        # launch_visual_production tool call carrying the edited args.
        kinds = [item.get("kind") for item in task_output["trajectory"]]
        assert "interrupt" in kinds
        names = [item.get("name") for item in task_output["trajectory"]]
        assert "launch_visual_production" in names

    def test_every_case_scores_1_0(self) -> None:
        experiment = build_approval_experiment()
        reports = experiment.run_evaluations(approval_task)
        for report in reports:
            assert report.overall_score == pytest.approx(1.0), (
                f"case {report.case.name} scored {report.overall_score}: "
                f"{report.reasons}"
            )
            assert all(report.test_passes), (
                f"case {report.case.name} failed: {report.reasons}"
            )

    def test_accept_case_has_expected_dispatched_args(self) -> None:
        experiment = build_approval_experiment()
        case = next(c for c in experiment.cases if c.name == "accept_visual_dispatch")
        assert case.metadata["expected_decision"] == "accept"
        assert case.metadata["post_approval_tool"] == "launch_visual_production"
        assert case.metadata["expected_tool_arguments"]["launch_visual_production"] == {
            "scene_id": "s1",
            "prompt": "cinematic cityscape at dawn, warm tones",
            "duration": 5.0,
        }

    def test_reject_case_short_circuits_downstream(self) -> None:
        experiment = build_approval_experiment()
        case = next(c for c in experiment.cases if c.name == "reject_visual_dispatch")
        trajectory_names = [
            item.get("name")
            for item in case.metadata["full_trajectory"]
            if item.get("kind") == "tool_call"
        ]
        assert "launch_assembly" not in trajectory_names
        assert "launch_b2_sync" not in trajectory_names

    def test_respond_case_short_circuits_downstream(self) -> None:
        experiment = build_approval_experiment()
        case = next(c for c in experiment.cases if c.name == "respond_assembly_hold")
        trajectory_names = [
            item.get("name")
            for item in case.metadata["full_trajectory"]
            if item.get("kind") == "tool_call"
        ]
        assert "launch_b2_sync" not in trajectory_names
