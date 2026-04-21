"""Direct-proof tests for :mod:`strands_agents.quanta.approval`."""

from __future__ import annotations

import pytest
from langgraph.types import Command

from strands_agents.quanta import (
    allowed_decisions_for,
    resume_command_from_decision,
    validate_decision,
)


class TestAllowedDecisionsFor:
    def test_launch_visual_production_allows_all_four(self) -> None:
        assert allowed_decisions_for("launch_visual_production") == {
            "accept",
            "edit",
            "reject",
            "respond",
        }

    def test_launch_assembly_disallows_edit(self) -> None:
        allowed = allowed_decisions_for("launch_assembly")
        assert "edit" not in allowed
        assert {"accept", "reject", "respond"}.issubset(allowed)

    def test_request_human_approval_gates_narrow(self) -> None:
        allowed = allowed_decisions_for("request_human_approval")
        assert "edit" not in allowed
        assert "accept" in allowed

    def test_unknown_tool_returns_empty_set(self) -> None:
        assert allowed_decisions_for("not_a_gate") == set()


class TestValidateDecision:
    def test_accept_on_production_gate_passes(self) -> None:
        validate_decision("launch_visual_production", {"type": "accept"})

    def test_edit_requires_args_dict(self) -> None:
        with pytest.raises(ValueError, match="dict 'args'"):
            validate_decision("launch_visual_production", {"type": "edit"})

    def test_edit_with_dict_args_passes(self) -> None:
        validate_decision(
            "launch_visual_production", {"type": "edit", "args": {"x": 1}}
        )

    def test_reject_requires_reason(self) -> None:
        with pytest.raises(ValueError, match="non-empty 'reason'"):
            validate_decision(
                "launch_visual_production", {"type": "reject", "reason": ""}
            )

    def test_respond_requires_content_key(self) -> None:
        with pytest.raises(ValueError, match="'content'"):
            validate_decision("launch_visual_production", {"type": "respond"})

    def test_edit_rejected_on_assembly_gate(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            validate_decision(
                "launch_assembly", {"type": "edit", "args": {"x": 1}}
            )

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown decision type"):
            validate_decision("launch_visual_production", {"type": "poke"})


class TestResumeCommandFromDecision:
    def test_returns_langgraph_command(self) -> None:
        cmd = resume_command_from_decision(
            "launch_visual_production", {"type": "accept"}
        )
        assert isinstance(cmd, Command)
        assert cmd.resume == {"type": "accept"}

    def test_validation_failure_propagates(self) -> None:
        with pytest.raises(ValueError):
            resume_command_from_decision(
                "launch_visual_production", {"type": "edit"}
            )
