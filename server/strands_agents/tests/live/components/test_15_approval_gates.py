"""Hermetic proof of robustness for Component 15 (approval-gates).

Clear-cut contracts proved here:

1. ``validate_decision`` accepts every canonical decision shape for
   every gate that allows it.
2. ``validate_decision`` rejects every decision a gate does *not*
   allow (e.g. ``edit`` on ``launch_assembly``, ``reject`` on
   ``request_human_approval``).
3. Each companion-key contract is enforced: ``edit`` requires dict
   ``args``, ``reject`` requires a non-empty ``reason``, ``respond``
   requires ``content``.
4. Unknown decision ``type`` raises ``ValueError``.
5. ``resume_command_from_decision`` rejects malformed decisions
   before constructing a :class:`Command`.
6. ``write_pending_envelope`` / ``write_approval_record`` round-trip
   through disk: payloads survive JSON, and the resume write wipes
   the matching pending envelope so the operator console cannot
   double-surface a resolved interrupt.
7. ``new_interrupt_id`` is unique across calls (monotonic enough for
   the run directory) and matches the canonical ``int-<12hex>``
   shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.types import Command

from strands_agents.approval import (
    INTERRUPT_GATE_CONFIG,
    ApprovalDecision,
    ApprovalRecord,
    _allowed_decisions,
    new_interrupt_id,
    resume_command_from_decision,
    validate_decision,
    write_approval_record,
    write_pending_envelope,
)


# ---------------------------------------------------------------------------
# Gate config is self-consistent
# ---------------------------------------------------------------------------


def test_gate_config_covers_the_three_canonical_tools() -> None:
    assert set(INTERRUPT_GATE_CONFIG) == {
        "launch_visual_production",
        "launch_assembly",
        "request_human_approval",
    }


def test_launch_visual_production_allows_all_four_decisions() -> None:
    allowed = _allowed_decisions("launch_visual_production")
    assert allowed == {"accept", "edit", "reject", "respond"}


def test_launch_assembly_forbids_edit() -> None:
    allowed = _allowed_decisions("launch_assembly")
    assert "edit" not in allowed
    assert {"accept", "reject", "respond"} <= allowed


def test_request_human_approval_is_accept_or_respond_only() -> None:
    allowed = _allowed_decisions("request_human_approval")
    assert allowed == {"accept", "respond"}


# ---------------------------------------------------------------------------
# validate_decision — accept paths
# ---------------------------------------------------------------------------


def test_accept_decision_is_valid_for_every_gate() -> None:
    for name in INTERRUPT_GATE_CONFIG:
        validate_decision(name, ApprovalDecision(type="accept"))


def test_edit_decision_is_valid_for_launch_visual_production() -> None:
    validate_decision(
        "launch_visual_production",
        ApprovalDecision(type="edit", args={"concept_ids": ["c-1", "c-2"]}),
    )


def test_reject_decision_is_valid_with_reason() -> None:
    validate_decision(
        "launch_visual_production",
        ApprovalDecision(type="reject", reason="style drift"),
    )


def test_respond_decision_is_valid_with_content() -> None:
    validate_decision(
        "request_human_approval",
        ApprovalDecision(type="respond", content="skip this scene"),
    )


# ---------------------------------------------------------------------------
# validate_decision — reject paths (vocabulary violations)
# ---------------------------------------------------------------------------


def test_unknown_decision_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown decision type"):
        validate_decision(
            "launch_visual_production",
            {"type": "reboot"},  # type: ignore[typeddict-item]
        )


def test_missing_decision_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown decision type"):
        validate_decision("launch_visual_production", {})  # type: ignore[arg-type]


def test_edit_on_launch_assembly_rejected() -> None:
    with pytest.raises(ValueError, match="not allowed for gate 'launch_assembly'"):
        validate_decision(
            "launch_assembly",
            ApprovalDecision(type="edit", args={"timeline_path": "/tmp/x.otio"}),
        )


def test_reject_on_request_human_approval_rejected() -> None:
    with pytest.raises(
        ValueError, match="not allowed for gate 'request_human_approval'"
    ):
        validate_decision(
            "request_human_approval",
            ApprovalDecision(type="reject", reason="operator busy"),
        )


# ---------------------------------------------------------------------------
# validate_decision — companion-key contracts
# ---------------------------------------------------------------------------


def test_edit_without_args_raises() -> None:
    with pytest.raises(ValueError, match="edit decision requires dict 'args'"):
        validate_decision(
            "launch_visual_production",
            {"type": "edit"},  # type: ignore[typeddict-item]
        )


def test_edit_with_non_dict_args_raises() -> None:
    with pytest.raises(ValueError, match="edit decision requires dict 'args'"):
        validate_decision(
            "launch_visual_production",
            {"type": "edit", "args": "not a dict"},  # type: ignore[typeddict-item]
        )


def test_reject_without_reason_raises() -> None:
    with pytest.raises(ValueError, match="reject decision requires non-empty 'reason'"):
        validate_decision(
            "launch_visual_production",
            {"type": "reject"},  # type: ignore[typeddict-item]
        )


def test_reject_with_blank_reason_raises() -> None:
    with pytest.raises(ValueError, match="reject decision requires non-empty 'reason'"):
        validate_decision(
            "launch_visual_production",
            ApprovalDecision(type="reject", reason="   "),
        )


def test_respond_without_content_raises() -> None:
    with pytest.raises(ValueError, match="respond decision requires 'content'"):
        validate_decision(
            "request_human_approval",
            {"type": "respond"},  # type: ignore[typeddict-item]
        )


# ---------------------------------------------------------------------------
# resume_command_from_decision
# ---------------------------------------------------------------------------


def test_resume_command_wraps_validated_decision() -> None:
    decision = ApprovalDecision(type="accept")
    command = resume_command_from_decision("launch_assembly", decision)
    assert isinstance(command, Command)
    assert command.resume == {"type": "accept"}


def test_resume_command_rejects_invalid_decision() -> None:
    with pytest.raises(ValueError):
        resume_command_from_decision(
            "launch_assembly",
            ApprovalDecision(type="edit", args={"x": 1}),
        )


# ---------------------------------------------------------------------------
# Audit trail round-trip
# ---------------------------------------------------------------------------


def test_pending_envelope_round_trip(tmp_path: Path) -> None:
    interrupt_id = "int-roundtrip01"
    path = write_pending_envelope(
        tmp_path,
        interrupt_id,
        "launch_visual_production",
        {"args": {"concepts": ["c-1"]}, "description": "approve shot"},
    )
    data = json.loads(path.read_text())
    assert data["interrupt_id"] == interrupt_id
    assert data["tool_name"] == "launch_visual_production"
    assert data["payload"]["description"] == "approve shot"
    assert "at" in data


def test_resume_record_wipes_matching_pending_envelope(tmp_path: Path) -> None:
    interrupt_id = "int-wipe000001"
    pending_path = write_pending_envelope(
        tmp_path,
        interrupt_id,
        "launch_assembly",
        {"args": {"timeline_path": "/tmp/x.otio"}},
    )
    assert pending_path.exists()

    record = ApprovalRecord(
        interrupt_id=interrupt_id,
        tool_name="launch_assembly",
        operator="operator-42",
        decision=ApprovalDecision(type="accept"),
    )
    resume_path = write_approval_record(tmp_path, record)

    assert resume_path.exists()
    assert not pending_path.exists(), (
        "resume write must wipe the pending envelope so the operator "
        "console does not surface a resolved interrupt twice"
    )

    data = json.loads(resume_path.read_text())
    assert data["interrupt_id"] == interrupt_id
    assert data["tool_name"] == "launch_assembly"
    assert data["operator"] == "operator-42"
    assert data["decision"] == {"type": "accept"}


def test_resume_record_without_pending_still_writes(tmp_path: Path) -> None:
    # CI replay case: a resume record can land without a prior pending
    # envelope (e.g. fixtures that only write the audit tail).
    record = ApprovalRecord(
        interrupt_id="int-naked00001",
        tool_name="request_human_approval",
        operator="operator-7",
        decision=ApprovalDecision(type="respond", content="skip"),
    )
    path = write_approval_record(tmp_path, record)
    assert path.exists()


# ---------------------------------------------------------------------------
# new_interrupt_id shape
# ---------------------------------------------------------------------------


def test_new_interrupt_id_has_canonical_shape() -> None:
    identifier = new_interrupt_id()
    assert identifier.startswith("int-")
    assert len(identifier) == len("int-") + 12


def test_new_interrupt_id_is_unique_across_calls() -> None:
    ids = {new_interrupt_id() for _ in range(256)}
    # 12 hex chars = 48 bits of entropy; duplicates here would be a
    # bug in the generator, not normal birthday collision range.
    assert len(ids) == 256
