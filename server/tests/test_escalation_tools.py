"""Tests for :mod:`orchestrator.escalation_tools`.

Uses an explicit :class:`ArtifactCritiqueStore` wired via the ``store=``
kwarg so the test suite does not depend on the module-level singleton.
Infra/provisioner lookups are stubbed via
:func:`set_infra_agent_factory` / :func:`set_provisioner_factory`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from critique.record import (  # noqa: E402
    Critique,
    EscalationRef,
    QaVerdict,
)
from critique.store import ArtifactCritiqueStore  # noqa: E402
from orchestrator import escalation_tools  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> ArtifactCritiqueStore:
    return ArtifactCritiqueStore(root=tmp_path, b2_enabled=False)


@pytest.fixture
def populated_store(store: ArtifactCritiqueStore) -> ArtifactCritiqueStore:
    # clip with 2 critiques, 1 qa verdict, 1 escalation
    store.append_critique(
        "clip", "s003_p002",
        Critique(source="visual_critic", rating="FAIR", summary="blurry"),
    )
    store.append_critique(
        "clip", "s003_p002",
        Critique(source="brand_voice_critic", rating="GOOD"),
    )
    store.append_qa(
        "clip", "s003_p002",
        QaVerdict(source="qa_jury", check_name="lip_sync", verdict="fail"),
    )
    store.append_escalation(
        "clip", "s003_p002",
        EscalationRef(scope_id="esc_1", action="regenerate_clip", outcome="failure"),
    )

    # scene with 1 critique and 1 qa pass
    store.append_critique(
        "scene", "s003",
        Critique(source="scenario_critic", rating="GOOD"),
    )
    store.append_qa(
        "scene", "s003",
        QaVerdict(source="scenario_evaluator", check_name="adhd", verdict="pass"),
    )
    return store


def test_read_artifact_critique_history_with_type(populated_store: ArtifactCritiqueStore):
    out = escalation_tools.read_artifact_critique_history(
        "s003_p002", "clip", store=populated_store,
    )
    assert len(out) == 2
    sources = {entry["source"] for entry in out}
    assert sources == {"visual_critic", "brand_voice_critic"}
    for entry in out:
        assert entry["artifact_type"] == "clip"
        assert entry["artifact_id"] == "s003_p002"


def test_read_artifact_critique_history_without_type(populated_store: ArtifactCritiqueStore):
    out = escalation_tools.read_artifact_critique_history(
        "s003_p002", store=populated_store,
    )
    assert len(out) == 2


def test_read_artifact_critique_history_sorted(populated_store: ArtifactCritiqueStore):
    out = escalation_tools.read_artifact_critique_history(
        "s003_p002", "clip", store=populated_store,
    )
    times = [e["timestamp"] for e in out]
    assert times == sorted(times)


def test_read_qa_verdicts(populated_store: ArtifactCritiqueStore):
    out = escalation_tools.read_qa_verdicts(
        "s003_p002", "clip", store=populated_store,
    )
    assert len(out) == 1
    assert out[0]["verdict"] == "fail"
    assert out[0]["source"] == "qa_jury"


def test_read_escalation_history_single_artifact(populated_store: ArtifactCritiqueStore):
    out = escalation_tools.read_escalation_history(
        "s003_p002", "clip", store=populated_store,
    )
    assert len(out) == 1
    assert out[0]["action"] == "regenerate_clip"
    assert out[0]["outcome"] == "failure"


def test_read_escalation_history_global(populated_store: ArtifactCritiqueStore):
    out = escalation_tools.read_escalation_history(store=populated_store)
    assert len(out) == 1


def test_read_escalation_history_empty(store: ArtifactCritiqueStore):
    assert escalation_tools.read_escalation_history(store=store) == []
    assert escalation_tools.read_escalation_history("missing", store=store) == []


def test_read_artifact_record_returns_dict(populated_store: ArtifactCritiqueStore):
    out = escalation_tools.read_artifact_record(
        "s003_p002", "clip", store=populated_store,
    )
    assert out is not None
    assert out["artifact_id"] == "s003_p002"
    assert len(out["critiques"]) == 2


def test_read_artifact_record_unknown_type(populated_store: ArtifactCritiqueStore):
    # Unknown artifact_type is silently tolerated (returns empty result)
    # so a misbehaving agent cannot crash the escalation path.
    out = escalation_tools.read_artifact_record(
        "s003_p002", "bogus", store=populated_store,
    )
    assert out is None


def test_read_artifact_record_missing(store: ArtifactCritiqueStore):
    assert escalation_tools.read_artifact_record("nope", "clip", store=store) is None


# ---------------------------------------------------------------------------
# Infra reads
# ---------------------------------------------------------------------------

class _FakeInfraAgent:
    def __init__(self, status: dict[str, Any]):
        self._status = status

    def get_status(self) -> dict[str, Any]:
        return self._status


def test_infra_tools_with_stub():
    status = {
        "paused": False,
        "workers": [
            {"url": "http://tts", "role": "tts", "status": "healthy"},
            {"url": "http://video", "role": "video", "status": "degraded"},
        ],
        "current_stage": {"name": "production", "elapsed_sec": 120, "expected_sec": 600, "ratio": 0.2},
        "recent_escalations": [
            {"severity": "warning", "source": "worker:video", "message": "VRAM high"},
            {"severity": "critical", "source": "stage:audio", "message": "slow"},
        ],
    }
    escalation_tools.set_infra_agent_factory(lambda: _FakeInfraAgent(status))
    try:
        snapshot = escalation_tools.read_infra_status_snapshot()
        assert snapshot == status

        workers = escalation_tools.read_worker_health()
        assert len(workers) == 2

        video_workers = escalation_tools.read_worker_health(role="video")
        assert len(video_workers) == 1
        assert video_workers[0]["status"] == "degraded"

        tts_workers = escalation_tools.read_worker_health(role="TTS")  # case-insensitive
        assert len(tts_workers) == 1

        timing = escalation_tools.read_stage_timing()
        assert timing["name"] == "production"

        log = escalation_tools.read_infra_escalation_log(limit=1)
        assert len(log) == 1
        assert log[0]["source"] == "stage:audio"
    finally:
        escalation_tools.set_infra_agent_factory(None)


def test_infra_tools_absent_infra_agent():
    # No infra agent installed — all tools return empty but do not crash.
    escalation_tools.set_infra_agent_factory(lambda: None)
    try:
        assert escalation_tools.read_infra_status_snapshot() == {}
        assert escalation_tools.read_worker_health() == []
        assert escalation_tools.read_stage_timing() == {}
        assert escalation_tools.read_infra_escalation_log() == []
    finally:
        escalation_tools.set_infra_agent_factory(None)


# ---------------------------------------------------------------------------
# Cost reads
# ---------------------------------------------------------------------------

class _FakeSpec:
    def __init__(self, role: str, max_price: float):
        self.role = role
        self.max_price = max_price


class _FakeProvisioner:
    def __init__(self, specs):
        self._specs = specs


def test_vast_cost_snapshot_with_specs(monkeypatch: pytest.MonkeyPatch):
    specs = [_FakeSpec("tts", 0.5), _FakeSpec("video", 1.2)]
    escalation_tools.set_provisioner_factory(lambda: _FakeProvisioner(specs))
    monkeypatch.setenv("VAST_BUDGET_REMAINING", "42.5")
    try:
        snap = escalation_tools.read_vast_cost_snapshot()
        assert snap["instances"] == 2
        assert snap["per_hour_usd"] == pytest.approx(1.7)
        assert snap["budget_remaining"] == pytest.approx(42.5)
        assert len(snap["spec_breakdown"]) == 2
    finally:
        escalation_tools.set_provisioner_factory(None)


def test_vast_cost_snapshot_absent_provisioner(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VAST_BUDGET_REMAINING", raising=False)
    escalation_tools.set_provisioner_factory(lambda: None)
    try:
        snap = escalation_tools.read_vast_cost_snapshot()
        # Only ``collected_at`` is guaranteed when there's nothing to report.
        assert "collected_at" in snap
        assert "per_hour_usd" not in snap
    finally:
        escalation_tools.set_provisioner_factory(None)


# ---------------------------------------------------------------------------
# Timeline state reads
# ---------------------------------------------------------------------------

def test_timeline_state_from_getter():
    def getter() -> dict[str, Any]:
        return {
            "user_prompt": "history of the potato",
            "scenes": [
                {"scene_num": 1, "duration_sec": 25.0, "narration_duration_sec": 24.5, "status": "ready"},
                {"scene_num": 2, "duration_sec": 30.0, "narration_duration_sec": 30.1, "status": "ready"},
            ],
            "visual_concepts": [{"..."}],
            "assembly": None,
        }

    out = escalation_tools.read_timeline_state(state_getter=getter)
    assert out["scene_count"] == 2
    assert out["has_visual_concepts"] is True
    assert out["has_assembly"] is False
    assert len(out["scenes"]) == 2
    assert out["scenes"][0]["scene_num"] == 1


def test_timeline_state_no_getter_returns_empty_shape():
    out = escalation_tools.read_timeline_state()
    # When neither getter nor B2 state is available the tool still returns
    # a stable shape rather than raising.
    assert out["scene_count"] == 0
    assert out["scenes"] == []


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------

def test_registry_names_match_module_exports():
    expected_names = {
        "read_artifact_critique_history",
        "read_qa_verdicts",
        "read_escalation_history",
        "read_artifact_record",
        "read_worker_health",
        "read_stage_timing",
        "read_infra_status_snapshot",
        "read_infra_escalation_log",
        "read_vast_cost_snapshot",
        "read_timeline_state",
    }
    registry_names = {name for name, _ in escalation_tools.ESCALATION_READ_TOOLS}
    assert registry_names == expected_names
    # every registered callable resolves to the module-level function.
    for name, fn in escalation_tools.ESCALATION_READ_TOOLS:
        assert getattr(escalation_tools, name) is fn
