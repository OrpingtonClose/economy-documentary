"""Tests for :mod:`critique.qa_storage` mirror helpers.

These tests use the on-disk :class:`ArtifactCritiqueStore` with a temp
root (no B2) and duck-typed evaluator stand-ins.  Focus is on:

* verdicts land in the artifact's record with correct source/check_name,
* ``replace_same_check`` scrubs prior verdicts for the same gate,
* helpers never raise when the adapter / store fails.
"""

from __future__ import annotations

import os
import sys

import pytest

_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from critique.qa_storage import (  # noqa: E402
    mirror_coherence_evaluator_result,
    mirror_gatekeeper_check,
    mirror_gatekeeper_checks,
    mirror_jury_verdict,
    mirror_scenario_evaluator_result,
    mirror_timeline_guardian_result,
)
from critique.store import ArtifactCritiqueStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeJuryVerdict:
    def __init__(
        self,
        *,
        artifact_id: str = "s003_p002",
        overall: str = "pass",
        confidence: float = 0.9,
        reasoning: str = "all checks passed",
        per_check_results: dict | None = None,
    ) -> None:
        self.artifact_id = artifact_id
        self.overall = overall
        self.confidence = confidence
        self.reasoning = reasoning
        self.per_check_results = per_check_results or {"pronunciation": "pass"}


class _FakeVerdictAttr:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeGatekeeperCheck:
    def __init__(
        self,
        *,
        name: str = "duration_match",
        verdict: str = "pass",
        stage: str = "clip",
        message: str = "ok",
        source: str = "gatekeeper",
    ) -> None:
        self.name = name
        self.verdict = _FakeVerdictAttr(verdict)
        self.stage = stage
        self.message = message
        self.source = source


@pytest.fixture()
def store(tmp_path) -> ArtifactCritiqueStore:
    return ArtifactCritiqueStore(root=tmp_path, b2_enabled=False)


# ---------------------------------------------------------------------------
# jury
# ---------------------------------------------------------------------------

def test_mirror_jury_verdict_appends_qa_record(store):
    jv = _FakeJuryVerdict(overall="pass")
    rec = mirror_jury_verdict(
        jv,
        artifact_type="clip",
        artifact_id="s003_p002",
        store=store,
        check_name="pronunciation",
    )
    assert rec is not None
    assert len(rec.qa_results) == 1
    verdict = rec.qa_results[0]
    assert verdict.source == "qa_jury"
    assert verdict.check_name == "pronunciation"
    assert verdict.verdict == "pass"


def test_mirror_jury_verdict_replace_same_check_scrubs_prior(store):
    mirror_jury_verdict(
        _FakeJuryVerdict(overall="fail", reasoning="initial fail"),
        artifact_type="clip",
        artifact_id="s003_p002",
        store=store,
        check_name="pronunciation",
    )
    rec = mirror_jury_verdict(
        _FakeJuryVerdict(overall="pass", reasoning="retry pass"),
        artifact_type="clip",
        artifact_id="s003_p002",
        store=store,
        check_name="pronunciation",
        replace_same_check=True,
    )
    assert rec is not None
    same_gate = [
        v for v in rec.qa_results
        if v.source == "qa_jury" and v.check_name == "pronunciation"
    ]
    assert len(same_gate) == 1
    assert same_gate[0].verdict == "pass"


def test_mirror_jury_verdict_without_replace_accumulates(store):
    for overall in ("fail", "pass"):
        mirror_jury_verdict(
            _FakeJuryVerdict(overall=overall),
            artifact_type="clip",
            artifact_id="s003_p002",
            store=store,
            check_name="pronunciation",
        )
    rec = store.read("clip", "s003_p002")
    assert rec is not None
    assert len(rec.qa_results) == 2


# ---------------------------------------------------------------------------
# gatekeeper
# ---------------------------------------------------------------------------

def test_mirror_gatekeeper_check_appends(store):
    check = _FakeGatekeeperCheck(name="duration_match", verdict="pass")
    rec = mirror_gatekeeper_check(
        check,
        artifact_type="clip",
        artifact_id="s001_p001",
        store=store,
    )
    assert rec is not None
    assert rec.qa_results[-1].source == "gatekeeper"
    assert rec.qa_results[-1].check_name == "duration_match"


def test_mirror_gatekeeper_checks_batch(store):
    checks = [
        _FakeGatekeeperCheck(name="duration_match", verdict="pass"),
        _FakeGatekeeperCheck(name="frame_count", verdict="warn"),
        _FakeGatekeeperCheck(name="freeze_frame", verdict="pass"),
    ]
    records = mirror_gatekeeper_checks(
        checks,
        artifact_type="clip",
        artifact_id="s001_p001",
        store=store,
    )
    assert len(records) == 3
    rec = store.read("clip", "s001_p001")
    assert rec is not None
    names = {v.check_name for v in rec.qa_results if v.source == "gatekeeper"}
    assert names == {"duration_match", "frame_count", "freeze_frame"}


# ---------------------------------------------------------------------------
# timeline_guardian
# ---------------------------------------------------------------------------

def test_mirror_timeline_guardian_result_pass(store):
    rec = mirror_timeline_guardian_result(
        "per_clip_duration",
        passed=True,
        artifact_type="assembly",
        artifact_id="final_cut",
        store=store,
        message="all clips within tolerance",
    )
    assert rec is not None
    v = rec.qa_results[-1]
    assert v.source == "timeline_guardian"
    assert v.check_name == "per_clip_duration"
    assert v.verdict == "pass"


def test_mirror_timeline_guardian_result_fail_includes_details(store):
    rec = mirror_timeline_guardian_result(
        "gaps",
        passed=False,
        artifact_type="assembly",
        artifact_id="final_cut",
        store=store,
        message="gap detected",
        details={"gap_seconds": 2.1},
    )
    assert rec is not None
    v = rec.qa_results[-1]
    assert v.verdict == "fail"
    assert v.details.get("gap_seconds") == 2.1


# ---------------------------------------------------------------------------
# scenario + coherence evaluators
# ---------------------------------------------------------------------------

def test_mirror_scenario_evaluator_replaces_on_reiteration(store):
    mirror_scenario_evaluator_result(
        "POOR",
        artifact_type="scenario",
        artifact_id="run-001",
        store=store,
        report="short opening",
    )
    rec = mirror_scenario_evaluator_result(
        "GOOD",
        artifact_type="scenario",
        artifact_id="run-001",
        store=store,
        report="acceptable",
    )
    assert rec is not None
    scenario_verdicts = [
        v for v in rec.qa_results if v.source == "scenario_evaluator"
    ]
    assert len(scenario_verdicts) == 1
    assert scenario_verdicts[0].verdict == "pass"
    assert scenario_verdicts[0].rating == "GOOD"


def test_mirror_coherence_evaluator_replaces_on_reiteration(store):
    mirror_coherence_evaluator_result(
        "FAIR",
        artifact_type="visual_concept",
        artifact_id="vc_s001",
        store=store,
        rationale="needs more vary",
    )
    rec = mirror_coherence_evaluator_result(
        "EXCELLENT",
        artifact_type="visual_concept",
        artifact_id="vc_s001",
        store=store,
        rationale="all good",
    )
    assert rec is not None
    coherence_verdicts = [
        v for v in rec.qa_results if v.source == "coherence_evaluator"
    ]
    assert len(coherence_verdicts) == 1
    assert coherence_verdicts[0].verdict == "pass"
    assert coherence_verdicts[0].rating == "EXCELLENT"


# ---------------------------------------------------------------------------
# Defensive: None store resolution + adapter failures
# ---------------------------------------------------------------------------

def test_mirror_helpers_return_none_when_store_is_unresolvable(monkeypatch, tmp_path):
    """When the default store accessor raises, the helper returns None."""
    import critique.qa_storage as qa_storage

    def _raise(*_a, **_kw):
        raise RuntimeError("no store available")

    monkeypatch.setattr(qa_storage, "get_critique_store", _raise)

    rec = mirror_jury_verdict(
        _FakeJuryVerdict(),
        artifact_type="clip",
        artifact_id="s003_p002",
        check_name="pronunciation",
    )
    assert rec is None


def test_mirror_jury_verdict_adapter_failure_does_not_raise(store):
    """A malformed jury verdict must not crash the mirror helper."""

    class _Broken:
        # accessing any attribute raises
        def __getattribute__(self, name):
            raise RuntimeError(f"boom accessing {name}")

    # Should return None but never raise.
    rec = mirror_jury_verdict(
        _Broken(),
        artifact_type="clip",
        artifact_id="s003_p002",
        store=store,
        check_name="pronunciation",
    )
    assert rec is None
