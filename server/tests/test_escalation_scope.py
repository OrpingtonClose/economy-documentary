"""Tests for :class:`orchestrator.escalation_scope.EscalationScope`."""

from __future__ import annotations

import os
import sys

import pytest

_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from orchestrator.escalation_menu import EscalationContext  # noqa: E402
from orchestrator.escalation_scope import (  # noqa: E402
    FAILURE_KINDS,
    EscalationScope,
    EscalationScopeError,
)


def test_minimal_scope_constructs_and_generates_id():
    s = EscalationScope(failure_kind="qa_fail", trigger_message="jury rejected clip")
    assert s.scope_id.startswith("esc_")
    assert s.created_at > 0
    assert s.failure_kind == "qa_fail"


def test_every_failure_kind_constructs():
    for kind in FAILURE_KINDS:
        s = EscalationScope(failure_kind=kind, trigger_message="m")  # type: ignore[arg-type]
        assert s.failure_kind == kind


def test_unknown_failure_kind_rejected():
    with pytest.raises(EscalationScopeError):
        EscalationScope(failure_kind="meltdown", trigger_message="m")  # type: ignore[arg-type]


def test_empty_trigger_rejected():
    with pytest.raises(EscalationScopeError):
        EscalationScope(failure_kind="qa_fail", trigger_message="")


def test_primary_id_requires_type():
    with pytest.raises(EscalationScopeError):
        EscalationScope(
            failure_kind="qa_fail",
            trigger_message="x",
            primary_artifact_id="c1",
        )


def test_invalid_artifact_type_rejected():
    with pytest.raises(EscalationScopeError):
        EscalationScope(
            failure_kind="qa_fail",
            trigger_message="x",
            primary_artifact_id="c1",
            primary_artifact_type="nope",  # type: ignore[arg-type]
        )


def test_tags_deduplicated_and_stripped():
    s = EscalationScope(
        failure_kind="qa_fail",
        trigger_message="x",
        scope_tags=["  vram_pressure ", "vram_pressure", "", "adhd_compliance"],
    )
    assert s.scope_tags == ["vram_pressure", "adhd_compliance"]


def test_summary_counters_rejects_non_int():
    with pytest.raises(EscalationScopeError):
        EscalationScope(
            failure_kind="qa_fail",
            trigger_message="x",
            summary_counters={"regen_count": 1.5},  # type: ignore[dict-item]
        )


def test_related_artifacts_validated():
    s = EscalationScope(
        failure_kind="qa_fail",
        trigger_message="x",
        related_artifacts=[("clip", "c1"), ["scene", "s3"]],  # type: ignore[list-item]
    )
    assert s.related_artifacts == [("clip", "c1"), ("scene", "s3")]

    with pytest.raises(EscalationScopeError):
        EscalationScope(
            failure_kind="qa_fail",
            trigger_message="x",
            related_artifacts=[("nope", "id")],
        )
    with pytest.raises(EscalationScopeError):
        EscalationScope(
            failure_kind="qa_fail",
            trigger_message="x",
            related_artifacts=[("clip",)],  # type: ignore[list-item]
        )
    with pytest.raises(EscalationScopeError):
        EscalationScope(
            failure_kind="qa_fail",
            trigger_message="x",
            related_artifacts=[("clip", "")],
        )


def test_roundtrip_via_dict():
    s = EscalationScope(
        failure_kind="worker_degraded",
        trigger_message="video worker at 95% VRAM for 3 min",
        stage_name="production",
        primary_artifact_id="c1",
        primary_artifact_type="clip",
        scope_tags=["gpu_worker_degraded", "vram_pressure"],
        summary_counters={"consecutive_failures": 4, "regen_count": 2},
        related_artifacts=[("clip", "c2"), ("clip", "c3")],
        high_cost=True,
    )
    data = s.to_dict()
    rebuilt = EscalationScope.from_dict(data)
    assert rebuilt.scope_id == s.scope_id
    assert rebuilt.failure_kind == "worker_degraded"
    assert rebuilt.high_cost is True
    assert rebuilt.related_artifacts == [("clip", "c2"), ("clip", "c3")]
    assert rebuilt.summary_counters == {"consecutive_failures": 4, "regen_count": 2}


def test_to_prompt_contains_expected_fields():
    s = EscalationScope(
        failure_kind="qa_fail",
        trigger_message="clip rejected by gatekeeper",
        stage_name="production",
        primary_artifact_id="s003_p002",
        primary_artifact_type="clip",
        scope_tags=["anti_cheat_trigger"],
        summary_counters={"regen_count": 2, "qa_fail_streak": 3},
        high_cost=True,
    )
    prompt = s.to_prompt()
    assert "ESCALATION SCOPE" in prompt
    assert s.scope_id in prompt
    assert "qa_fail" in prompt
    assert "production" in prompt
    assert "clip:s003_p002" in prompt
    assert "anti_cheat_trigger" in prompt
    assert "regen_count=2" in prompt
    assert "qa_fail_streak=3" in prompt
    assert "high_cost" in prompt


# ---------------------------------------------------------------------------
# EscalationScope.from_context bridge (PR-2)
# ---------------------------------------------------------------------------

def test_from_context_translates_legacy_fields():
    ctx = EscalationContext(
        failing_artifact="production clip c-7 failed jury",
        artifact_descriptor={"clip_id": "c-7", "seed": 42},
        timeline_state_snapshot={"total_sec": 120.0},
        escalation_history=[{"action": "regenerate_clip"}],
        high_cost=True,
    )
    scope = EscalationScope.from_context(
        ctx,
        failure_kind="qa_fail",
        primary_artifact_id="c-7",
        primary_artifact_type="clip",
        scope_tags=["jury_split"],
    )

    assert scope.failure_kind == "qa_fail"
    assert scope.trigger_message == ctx.failing_artifact
    assert scope.primary_artifact_id == "c-7"
    assert scope.primary_artifact_type == "clip"
    assert scope.high_cost is True
    assert "jury_split" in scope.scope_tags
    # Default stage_name inferred from the first token of failing_artifact.
    assert scope.stage_name == "production"
    assert scope.summary_counters["prior_escalations"] == 1
    # Legacy push-side fields are preserved (not lost) in metadata.
    legacy = scope.metadata.get("legacy_context")
    assert isinstance(legacy, dict)
    assert legacy["artifact_descriptor"]["clip_id"] == "c-7"


def test_from_context_rejects_none():
    with pytest.raises(EscalationScopeError):
        EscalationScope.from_context(None)  # type: ignore[arg-type]


def test_from_context_defaults_when_minimal():
    ctx = EscalationContext(
        failing_artifact="",
        artifact_descriptor={},
        timeline_state_snapshot={},
        escalation_history=[],
        high_cost=False,
    )
    scope = EscalationScope.from_context(ctx)
    assert scope.trigger_message == "unknown failure"
    assert scope.primary_artifact_id is None
    assert scope.high_cost is False
