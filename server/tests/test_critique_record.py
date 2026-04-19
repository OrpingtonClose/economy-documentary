"""Tests for :mod:`critique.record` dataclasses.

Covers:
    - Critique validation + round-trip via to_dict / from_dict
    - QaVerdict validation + round-trip
    - EscalationRef validation + round-trip
    - ArtifactCritiqueRecord round-trip + aggregate views
"""

from __future__ import annotations

import os
import sys

import pytest

_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from critique.record import (  # noqa: E402
    ARTIFACT_TYPES,
    ArtifactCritiqueRecord,
    Critique,
    EscalationRef,
    QA_VERDICTS,
    QaVerdict,
    worst_status,
)


# ---------------------------------------------------------------------------
# Critique
# ---------------------------------------------------------------------------

def test_critique_requires_source():
    with pytest.raises(ValueError):
        Critique(source="", rating="GOOD")


def test_critique_rejects_unknown_rating():
    with pytest.raises(ValueError):
        Critique(source="scenario_critic", rating="PERFECT")  # type: ignore[arg-type]


def test_critique_roundtrip():
    c = Critique(
        source="scenario_critic",
        voter_model="gemini-2.5-flash",
        rating="GOOD",
        score=0.82,
        summary="Solid three-act structure.",
        issues=["pacing drag in act 2"],
        suggestions=["compress act 2 by 10%"],
        details={"per_check": {"pacing": "WARN"}},
    )
    data = c.to_dict()
    rebuilt = Critique.from_dict(data)
    assert rebuilt.source == "scenario_critic"
    assert rebuilt.rating == "GOOD"
    assert rebuilt.score == 0.82
    assert rebuilt.issues == ["pacing drag in act 2"]
    assert rebuilt.suggestions == ["compress act 2 by 10%"]
    assert rebuilt.details == {"per_check": {"pacing": "WARN"}}
    assert rebuilt.timestamp > 0


def test_critique_timestamp_defaults_to_now():
    c = Critique(source="x", rating="UNKNOWN")
    assert c.timestamp > 0


# ---------------------------------------------------------------------------
# QaVerdict
# ---------------------------------------------------------------------------

def test_qa_verdict_validates_status():
    with pytest.raises(ValueError):
        QaVerdict(source="qa_jury", check_name="x", verdict="broken")  # type: ignore[arg-type]


def test_qa_verdict_rejects_bad_confidence():
    with pytest.raises(ValueError):
        QaVerdict(source="qa_jury", check_name="x", verdict="pass", confidence=1.5)
    with pytest.raises(ValueError):
        QaVerdict(source="qa_jury", check_name="x", verdict="pass", confidence=-0.1)


def test_qa_verdict_roundtrip_all_statuses():
    for status in QA_VERDICTS:
        q = QaVerdict(
            source="gatekeeper",
            check_name="anti_cheat",
            verdict=status,  # type: ignore[arg-type]
            confidence=0.75,
            rating="GOOD",
            message="looks fine",
            details={"stage": "production"},
        )
        data = q.to_dict()
        rebuilt = QaVerdict.from_dict(data)
        assert rebuilt.verdict == status
        assert rebuilt.confidence == 0.75
        assert rebuilt.details["stage"] == "production"


# ---------------------------------------------------------------------------
# EscalationRef
# ---------------------------------------------------------------------------

def test_escalation_ref_requires_action_and_scope():
    with pytest.raises(ValueError):
        EscalationRef(scope_id="", action="regenerate_clip")
    with pytest.raises(ValueError):
        EscalationRef(scope_id="esc_abc", action="")


def test_escalation_ref_roundtrip():
    e = EscalationRef(
        scope_id="esc_xyz",
        action="regenerate_clip",
        outcome="success",
        reasoning="prompt_delta pushed toward kitchen setting",
    )
    data = e.to_dict()
    rebuilt = EscalationRef.from_dict(data)
    assert rebuilt.scope_id == "esc_xyz"
    assert rebuilt.action == "regenerate_clip"
    assert rebuilt.outcome == "success"


def test_escalation_ref_rejects_bad_outcome():
    with pytest.raises(ValueError):
        EscalationRef(scope_id="esc_x", action="regenerate_clip", outcome="soso")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# worst_status
# ---------------------------------------------------------------------------

def test_worst_status_ordering():
    assert worst_status([]) == "pass"
    assert worst_status(["pass", "pass"]) == "pass"
    assert worst_status(["pass", "warn"]) == "warn"
    assert worst_status(["warn", "escalate"]) == "escalate"
    assert worst_status(["warn", "escalate", "fail"]) == "fail"
    assert worst_status(["fail", "pass"]) == "fail"


# ---------------------------------------------------------------------------
# ArtifactCritiqueRecord
# ---------------------------------------------------------------------------

def test_record_validates_artifact_type():
    with pytest.raises(ValueError):
        ArtifactCritiqueRecord(artifact_type="nope", artifact_id="x")  # type: ignore[arg-type]


def test_record_requires_id():
    with pytest.raises(ValueError):
        ArtifactCritiqueRecord(artifact_type="scene", artifact_id="")


def test_record_every_artifact_type_roundtrips():
    for t in ARTIFACT_TYPES:
        r = ArtifactCritiqueRecord(artifact_type=t, artifact_id=f"id_{t}")  # type: ignore[arg-type]
        data = r.to_dict()
        rebuilt = ArtifactCritiqueRecord.from_dict(data)
        assert rebuilt.artifact_type == t
        assert rebuilt.artifact_id == f"id_{t}"


def test_record_aggregate_views():
    r = ArtifactCritiqueRecord(
        artifact_type="clip",
        artifact_id="s003_p002",
        iteration=2,
        produced_by="production_orchestrator",
        critiques=[Critique(source="visual_critic", rating="FAIR")],
        qa_results=[
            QaVerdict(source="qa_jury", check_name="pronunciation", verdict="pass"),
            QaVerdict(source="gatekeeper", check_name="duration_match", verdict="warn"),
        ],
        escalations=[
            EscalationRef(scope_id="esc_1", action="regenerate_clip", outcome="failure",
                          timestamp=10.0),
            EscalationRef(scope_id="esc_2", action="freeze_frame_fill", outcome="success",
                          timestamp=20.0),
        ],
    )
    assert r.worst_qa() == "warn"
    latest = r.latest_escalation()
    assert latest is not None
    assert latest.scope_id == "esc_2"

    # round-trip preserves the aggregates.
    rebuilt = ArtifactCritiqueRecord.from_dict(r.to_dict())
    assert rebuilt.worst_qa() == "warn"
    assert rebuilt.latest_escalation() is not None
    assert rebuilt.latest_escalation().scope_id == "esc_2"  # type: ignore[union-attr]
