"""ARCH-G3 (issue #155) — preview consumer tests.

Exercises :mod:`server.previews.consumers`:

- ``evaluate_preview`` derives structured findings from the manifest
  and routes them to the content ladder via
  :func:`recovery.submit_escalation`,
- ``emit_preview_ready`` pushes a ``preview_ready`` SSE event on the
  AG-UI dashboard channel,
- ``handle_human_dislike_preview`` escalates to proactive L4 via the
  same ``submit_escalation`` path,
- consumers do not mutate OTIO / pipeline state (non-advancement
  invariant).
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest import mock

import pytest

import recovery
from previews.builder import (
    PREVIEW_ARTIFACT_KIND,
    PREVIEW_HISTORY_KEY,
    PreviewManifest,
    SlotKind,
    SlotPlan,
    SlotStatus,
)
from previews.consumers import (
    AGENT_ESCALATION_OP,
    ESCALATION_LEVEL_CONTENT,
    ESCALATION_LEVEL_L4,
    HUMAN_DISLIKE_ESCALATION_OP,
    PREVIEW_READY_EVENT,
    emit_preview_ready,
    evaluate_preview,
    handle_human_dislike_preview,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _slot(
    *,
    slot_key: str,
    kind: SlotKind = SlotKind.VIDEO,
    status: SlotStatus = SlotStatus.DELIVERED,
    duration_sec: float = 1.0,
    scene_num: int | None = 1,
    scripted_text: str | None = None,
    failure_reason: str | None = None,
    rung_text: str | None = None,
    eta_text: str | None = None,
    media_path: str | None = None,
    index: int = 0,
    track: str = "V1_Video",
) -> SlotPlan:
    return SlotPlan(
        track=track,
        kind=kind,
        index=index,
        slot_key=slot_key,
        status=status,
        duration_sec=duration_sec,
        media_path=media_path,
        scene_num=scene_num,
        scripted_text=scripted_text,
        eta_text=eta_text,
        rung_text=rung_text,
        failure_reason=failure_reason,
    )


def _write_manifest(tmp_path, manifest: PreviewManifest) -> str:
    data = manifest.to_dict()
    path = str(tmp_path / "preview.manifest.json")
    with open(path, "w") as fh:
        json.dump(data, fh)
    preview_path = str(tmp_path / "preview.mp4")
    with open(preview_path, "wb") as fh:
        fh.write(b"fake-mp4")
    return preview_path


@pytest.fixture
def manifest_with_mixed_slots(tmp_path):
    slots = (
        _slot(slot_key="scene_001_V1", status=SlotStatus.DELIVERED),
        _slot(
            slot_key="scene_002_V1",
            status=SlotStatus.MISSING,
            eta_text="ETA: 3.0 min",
        ),
        _slot(
            slot_key="scene_003_V1",
            status=SlotStatus.FAILED,
            failure_reason="ladder exhausted",
        ),
        _slot(
            slot_key="scene_004_V1",
            status=SlotStatus.IN_PROGRESS,
            rung_text="L2 CREATIVE — trying alt provider",
        ),
    )
    counts: dict[str, int] = {}
    for s in slots:
        counts[s.status.value] = counts.get(s.status.value, 0) + 1
    manifest = PreviewManifest(
        kind=PREVIEW_ARTIFACT_KIND,
        preview_path=str(tmp_path / "preview.mp4"),
        manifest_path=str(tmp_path / "preview.manifest.json"),
        input_hash="hash_mixed",
        trigger_reason="scene_004_complete",
        timeline_path=str(tmp_path / "timeline.otio"),
        otio_state="draft",
        built_at=0.0,
        total_duration_sec=4.0,
        slots=slots,
        counts=counts,
    )
    preview_path = _write_manifest(tmp_path, manifest)
    return manifest, preview_path


@pytest.fixture
def manifest_all_delivered(tmp_path):
    slots = (
        _slot(slot_key="scene_001_V1", status=SlotStatus.DELIVERED),
        _slot(slot_key="scene_002_V1", status=SlotStatus.DELIVERED),
    )
    manifest = PreviewManifest(
        kind=PREVIEW_ARTIFACT_KIND,
        preview_path=str(tmp_path / "preview.mp4"),
        manifest_path=str(tmp_path / "preview.manifest.json"),
        input_hash="hash_ok",
        trigger_reason="act_001_complete",
        timeline_path=str(tmp_path / "timeline.otio"),
        otio_state="draft",
        built_at=0.0,
        total_duration_sec=2.0,
        slots=slots,
        counts={SlotStatus.DELIVERED.value: 2},
    )
    preview_path = _write_manifest(tmp_path, manifest)
    return manifest, preview_path


@pytest.fixture(autouse=True)
def _clear_escalations():
    """Ensure each test sees a clean escalation registry."""
    with recovery._escalation_lock:
        recovery._pending_escalations.clear()
        recovery._escalation_counter = 0
    yield
    with recovery._escalation_lock:
        recovery._pending_escalations.clear()
        recovery._escalation_counter = 0


# ---------------------------------------------------------------------------
# evaluate_preview — agent lane
# ---------------------------------------------------------------------------


class TestEvaluatePreview:

    def test_returns_findings_for_mixed_slots(
        self, manifest_with_mixed_slots
    ):
        _, preview_path = manifest_with_mixed_slots
        result = evaluate_preview({}, preview_path=preview_path)
        assert result["trigger_reason"] == "scene_004_complete"
        assert result["input_hash"] == "hash_mixed"
        kinds = {f["status"] for f in result["findings"]}
        # delivered is NOT a finding.
        assert SlotStatus.DELIVERED.value not in kinds
        assert SlotStatus.FAILED.value in kinds
        assert SlotStatus.MISSING.value in kinds
        assert SlotStatus.IN_PROGRESS.value in kinds

    def test_failed_finding_is_critical(self, manifest_with_mixed_slots):
        _, preview_path = manifest_with_mixed_slots
        result = evaluate_preview({}, preview_path=preview_path)
        failed = [
            f for f in result["findings"]
            if f["status"] == SlotStatus.FAILED.value
        ]
        assert failed
        assert failed[0]["severity"] == "critical"
        assert "ladder exhausted" in failed[0]["reason"]

    def test_no_findings_when_all_delivered(self, manifest_all_delivered):
        _, preview_path = manifest_all_delivered
        result = evaluate_preview({}, preview_path=preview_path)
        assert result["findings"] == []
        assert result["escalated"] is False
        assert result["escalation_level"] is None

    def test_escalation_is_submitted_with_content_ladder_level(
        self, manifest_with_mixed_slots
    ):
        _, preview_path = manifest_with_mixed_slots
        with mock.patch.object(
            recovery, "submit_escalation", wraps=recovery.submit_escalation
        ) as mock_submit:
            result = evaluate_preview({}, preview_path=preview_path)
        assert result["escalated"] is True
        assert result["escalation_level"] == ESCALATION_LEVEL_CONTENT
        assert mock_submit.call_count == 1
        req = mock_submit.call_args.args[0]
        assert req.operation_name.startswith(AGENT_ESCALATION_OP)
        assert req.severity == "critical"
        assert req.diagnosis["level"] == ESCALATION_LEVEL_CONTENT
        assert req.diagnosis["preview_path"] == preview_path

    def test_escalation_severity_warning_without_failures(self, tmp_path):
        slots = (
            _slot(slot_key="scene_001_V1", status=SlotStatus.DELIVERED),
            _slot(
                slot_key="scene_002_V1",
                status=SlotStatus.MISSING,
                eta_text="ETA: 1 min",
            ),
        )
        manifest = PreviewManifest(
            kind=PREVIEW_ARTIFACT_KIND,
            preview_path=str(tmp_path / "preview.mp4"),
            manifest_path=str(tmp_path / "preview.manifest.json"),
            input_hash="h",
            trigger_reason="scene_001_complete",
            timeline_path="t",
            otio_state="draft",
            built_at=0.0,
            total_duration_sec=2.0,
            slots=slots,
            counts={"delivered": 1, "missing": 1},
        )
        preview_path = _write_manifest(tmp_path, manifest)
        with mock.patch.object(
            recovery, "submit_escalation", wraps=recovery.submit_escalation
        ) as mock_submit:
            evaluate_preview({}, preview_path=preview_path)
        req = mock_submit.call_args.args[0]
        assert req.severity == "warning"

    def test_uses_latest_preview_from_state(self, manifest_with_mixed_slots):
        _, preview_path = manifest_with_mixed_slots
        state = {"_latest_preview_path": preview_path}
        result = evaluate_preview(state)
        assert result["preview_path"] == preview_path

    def test_raises_without_path(self):
        with pytest.raises(ValueError):
            evaluate_preview({})

    def test_rejects_non_preview_manifest(self, tmp_path):
        path = str(tmp_path / "preview.manifest.json")
        with open(path, "w") as fh:
            json.dump({"kind": "deliverable"}, fh)
        with open(tmp_path / "preview.mp4", "wb") as fh:
            fh.write(b"x")
        with pytest.raises(ValueError):
            evaluate_preview({}, preview_path=str(tmp_path / "preview.mp4"))

    def test_digest_is_deterministic(self, manifest_with_mixed_slots):
        _, preview_path = manifest_with_mixed_slots
        d1 = evaluate_preview({}, preview_path=preview_path)["digest"]
        d2 = evaluate_preview({}, preview_path=preview_path)["digest"]
        assert d1 == d2


# ---------------------------------------------------------------------------
# emit_preview_ready — human lane
# ---------------------------------------------------------------------------


class TestEmitPreviewReady:

    def test_emits_preview_ready_event(self, manifest_with_mixed_slots):
        manifest, _ = manifest_with_mixed_slots
        with mock.patch("agui.emit_agui_event") as mock_emit:
            emit_preview_ready(manifest)
        assert mock_emit.call_count == 1
        event_type, payload = mock_emit.call_args.args
        assert event_type == PREVIEW_READY_EVENT
        assert payload["preview_path"] == manifest.preview_path
        assert payload["trigger_reason"] == "scene_004_complete"
        assert payload["input_hash"] == "hash_mixed"
        assert payload["kind"] == PREVIEW_ARTIFACT_KIND
        assert "digest" in payload
        assert payload["counts"][SlotStatus.FAILED.value] == 1

    def test_swallows_emit_failure(self, manifest_with_mixed_slots):
        manifest, _ = manifest_with_mixed_slots
        with mock.patch(
            "agui.emit_agui_event", side_effect=RuntimeError("SSE down")
        ):
            # Must not raise — dashboard emission is best-effort.
            emit_preview_ready(manifest)


# ---------------------------------------------------------------------------
# handle_human_dislike_preview — proactive L4 escalation
# ---------------------------------------------------------------------------


class TestHumanDislike:

    def test_escalates_with_l4_level(self, manifest_with_mixed_slots):
        _, preview_path = manifest_with_mixed_slots
        event = {
            "preview_path": preview_path,
            "reason": "pacing feels off in act 1",
            "reviewer": "producer-01",
            "trigger_reason": "scene_004_complete",
        }
        with mock.patch.object(
            recovery, "submit_escalation", wraps=recovery.submit_escalation
        ) as mock_submit:
            escalation_id = handle_human_dislike_preview(event)
        assert escalation_id is not None
        assert mock_submit.call_count == 1
        req = mock_submit.call_args.args[0]
        assert req.operation_name.startswith(HUMAN_DISLIKE_ESCALATION_OP)
        assert req.diagnosis["level"] == ESCALATION_LEVEL_L4
        assert req.diagnosis["reviewer"] == "producer-01"
        assert "pacing feels off" in req.diagnosis["root_cause"]
        assert req.severity == "critical"
        # Proposed actions include regenerate + reconsider.
        action_ids = {a["action_id"] for a in req.proposed_actions}
        assert "regenerate_flagged_scenes" in action_ids
        assert "scenario_director_reconsider" in action_ids

    def test_attaches_escalation_id_to_history(
        self, manifest_with_mixed_slots
    ):
        manifest, preview_path = manifest_with_mixed_slots
        state: dict[str, Any] = {
            PREVIEW_HISTORY_KEY: [manifest.to_dict()],
        }
        escalation_id = handle_human_dislike_preview(
            {"preview_path": preview_path, "reason": "meh"},
            state=state,
        )
        assert state[PREVIEW_HISTORY_KEY][-1][
            "human_dislike_escalation_id"
        ] == escalation_id

    def test_unknown_preview_still_escalates(self, tmp_path):
        """Even if the manifest file is missing, a human dislike must
        still escalate — the reviewer's signal is authoritative."""
        event = {
            "preview_path": str(tmp_path / "nonexistent.mp4"),
            "reason": "bad",
        }
        with mock.patch.object(
            recovery, "submit_escalation", wraps=recovery.submit_escalation
        ) as mock_submit:
            escalation_id = handle_human_dislike_preview(event)
        assert escalation_id is not None
        assert mock_submit.call_count == 1


# ---------------------------------------------------------------------------
# Non-advancement invariant — consumers must not mutate OTIO or phase
# ---------------------------------------------------------------------------


class TestNonAdvancement:

    def test_evaluate_preview_does_not_mutate_state(
        self, manifest_with_mixed_slots
    ):
        _, preview_path = manifest_with_mixed_slots
        state: dict[str, Any] = {
            "_latest_preview_path": preview_path,
            "pipeline_phase": "production",
            "approved_stages": {"audio": True},
        }
        before = json.dumps(state, sort_keys=True, default=str)
        evaluate_preview(state)
        after = json.dumps(state, sort_keys=True, default=str)
        assert before == after, (
            "evaluate_preview mutated pipeline state"
        )

    def test_handle_dislike_only_writes_escalation_id(
        self, manifest_with_mixed_slots
    ):
        manifest, preview_path = manifest_with_mixed_slots
        state: dict[str, Any] = {
            "pipeline_phase": "production",
            "approved_stages": {"audio": True},
            PREVIEW_HISTORY_KEY: [manifest.to_dict()],
        }
        before_phase = state["pipeline_phase"]
        before_approved = dict(state["approved_stages"])
        handle_human_dislike_preview(
            {"preview_path": preview_path, "reason": "meh"}, state=state
        )
        assert state["pipeline_phase"] == before_phase
        assert state["approved_stages"] == before_approved


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_escalation_level_constants():
    assert ESCALATION_LEVEL_L4 == "L4"
    assert ESCALATION_LEVEL_CONTENT == "content_ladder"


def test_preview_ready_event_kind():
    assert PREVIEW_READY_EVENT == "preview_ready"


def test_consumers_do_not_import_opentimelineio():
    """Consumer lane works off the manifest only — no OTIO dependency."""
    import importlib

    mod = importlib.import_module("previews.consumers")
    # OTIO must not be required at module level for consumers.
    assert "opentimelineio" not in getattr(mod, "__dict__", {})


def test_manifest_file_path(manifest_with_mixed_slots):
    _, preview_path = manifest_with_mixed_slots
    expected = preview_path[: -len(".mp4")] + ".manifest.json"
    assert os.path.exists(expected)
