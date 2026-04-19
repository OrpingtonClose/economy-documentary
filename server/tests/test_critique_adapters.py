"""Tests for :mod:`critique.adapters`.

Adapters are duck-typed: each accepts any object with the right attribute
surface.  Tests use lightweight fakes rather than pulling in the real
evaluator modules so this file stays fast and hermetic.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

import pytest

_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from critique.adapters import (  # noqa: E402
    coherence_evaluator_to_qa,
    critic_payload_to_critique,
    gatekeeper_to_qa,
    jury_to_qa,
    rating_to_critique_rating,
    rating_to_verdict,
    scenario_evaluator_to_qa,
    timeline_guardian_to_qa,
)


# ---------------------------------------------------------------------------
# rating_* helpers
# ---------------------------------------------------------------------------

def test_rating_to_verdict_mapping():
    assert rating_to_verdict("EXCELLENT") == "pass"
    assert rating_to_verdict("GOOD") == "pass"
    assert rating_to_verdict("FAIR") == "warn"
    assert rating_to_verdict("POOR") == "fail"
    assert rating_to_verdict("UNKNOWN") == "escalate"
    assert rating_to_verdict("  good  ") == "pass"
    # Unknown rating escalates rather than silently passing.
    assert rating_to_verdict("stellar") == "escalate"
    assert rating_to_verdict("") == "escalate"


def test_rating_to_critique_rating_mapping():
    assert rating_to_critique_rating("EXCELLENT") == "EXCELLENT"
    assert rating_to_critique_rating("good") == "GOOD"
    assert rating_to_critique_rating("unheard-of") == "UNKNOWN"
    assert rating_to_critique_rating("") == "UNKNOWN"


# ---------------------------------------------------------------------------
# jury_to_qa
# ---------------------------------------------------------------------------

@dataclass
class _FakeJury:
    artifact_id: str
    overall: str = "pass"
    reasoning: str = ""
    confidence: float = 1.0
    per_check_results: dict = field(default_factory=dict)


def test_jury_to_qa_preserves_overall_and_details():
    jury = _FakeJury(
        artifact_id="s003_p002",
        overall="fail",
        reasoning="3 voters rejected lip sync",
        confidence=0.33,
        per_check_results={"lip_sync": "fail", "pronunciation": "pass"},
    )
    q = jury_to_qa(jury)
    assert q.source == "qa_jury"
    assert q.verdict == "fail"
    assert q.confidence == pytest.approx(0.33)
    assert "lip_sync" in q.details["per_check_results"]
    assert q.details["artifact_id"] == "s003_p002"


def test_jury_to_qa_unknown_overall_escalates():
    jury = _FakeJury(artifact_id="x", overall="weird")
    q = jury_to_qa(jury)
    assert q.verdict == "escalate"


def test_jury_to_qa_uses_check_name_fallback():
    jury = _FakeJury(artifact_id="x", per_check_results={"pace": "pass"})
    q = jury_to_qa(jury)
    assert q.check_name == "pace"

    jury_empty = _FakeJury(artifact_id="x")
    q2 = jury_to_qa(jury_empty)
    assert q2.check_name == "jury"


def test_jury_to_qa_clamps_confidence():
    jury = _FakeJury(artifact_id="x", confidence=5.0)
    q = jury_to_qa(jury)
    assert q.confidence == 1.0


# ---------------------------------------------------------------------------
# gatekeeper_to_qa
# ---------------------------------------------------------------------------

@dataclass
class _FakeGatekeeperCheck:
    name: str = "anti_cheat"
    category: str = "anti_cheat"
    verdict: Any = "pass"
    message: str = ""
    stage: str = ""
    scene_num: int = 0
    phrase_idx: int = 0
    metadata: dict = field(default_factory=dict)
    timestamp: float = 0.0


def test_gatekeeper_reject_maps_to_fail():
    c = _FakeGatekeeperCheck(verdict="reject", message="duplicate clip detected")
    q = gatekeeper_to_qa(c)
    assert q.source == "gatekeeper"
    assert q.verdict == "fail"
    assert q.message == "duplicate clip detected"


def test_gatekeeper_pass_and_warn_preserved():
    assert gatekeeper_to_qa(_FakeGatekeeperCheck(verdict="pass")).verdict == "pass"
    assert gatekeeper_to_qa(_FakeGatekeeperCheck(verdict="warn")).verdict == "warn"


def test_gatekeeper_supports_enum_value():
    class _Enum:
        value = "reject"

    c = _FakeGatekeeperCheck(verdict=_Enum())
    q = gatekeeper_to_qa(c)
    assert q.verdict == "fail"


# ---------------------------------------------------------------------------
# timeline_guardian_to_qa
# ---------------------------------------------------------------------------

def test_timeline_guardian_passed():
    q = timeline_guardian_to_qa("gap_zero", True, message="no gaps")
    assert q.verdict == "pass"
    assert q.check_name == "gap_zero"


def test_timeline_guardian_failed():
    q = timeline_guardian_to_qa(
        "duration_monotonic", False,
        message="scene 3 exceeds expected",
        details={"scene_num": 3},
    )
    assert q.verdict == "fail"
    assert q.details["scene_num"] == 3


# ---------------------------------------------------------------------------
# scenario_evaluator_to_qa
# ---------------------------------------------------------------------------

def test_scenario_evaluator_mapping():
    q = scenario_evaluator_to_qa("GOOD", report="pacing fine")
    assert q.verdict == "pass"
    assert q.rating == "GOOD"
    assert q.message == "pacing fine"


def test_scenario_evaluator_records_structural_cap():
    q = scenario_evaluator_to_qa("POOR", structural_cap="POOR")
    assert q.verdict == "fail"
    assert q.details["structural_cap"] == "POOR"


# ---------------------------------------------------------------------------
# coherence_evaluator_to_qa
# ---------------------------------------------------------------------------

def test_coherence_evaluator_mapping():
    q = coherence_evaluator_to_qa("FAIR", rationale="some shots inconsistent")
    assert q.verdict == "warn"
    assert q.rating == "FAIR"


# ---------------------------------------------------------------------------
# critic_payload_to_critique
# ---------------------------------------------------------------------------

def test_critic_payload_to_critique_typical():
    c = critic_payload_to_critique(
        {
            "rating": "FAIR",
            "score": 0.5,
            "summary": "pacing drags in act 2",
            "issues": ["long takes in scene 3"],
            "suggestions": ["trim scene 3 by 2s"],
            "extra": {"notes": "watch for kitchen continuity"},
        },
        source="scenario_critic",
        voter_model="gemini-2.5-flash",
    )
    assert c.source == "scenario_critic"
    assert c.rating == "FAIR"
    assert c.score == 0.5
    assert c.issues == ["long takes in scene 3"]
    assert "extra" in c.details


def test_critic_payload_to_critique_tolerates_missing_fields():
    c = critic_payload_to_critique({}, source="x")
    assert c.rating == "UNKNOWN"
    assert c.score is None
    assert c.issues == []
    assert c.suggestions == []


def test_critic_payload_to_critique_str_issues_normalised_to_list():
    c = critic_payload_to_critique(
        {"issues": "single issue", "suggestions": None}, source="x",
    )
    assert c.issues == ["single issue"]
    assert c.suggestions == []


def test_critic_payload_to_critique_bad_score_becomes_none():
    c = critic_payload_to_critique({"score": "not a number"}, source="x")
    assert c.score is None
