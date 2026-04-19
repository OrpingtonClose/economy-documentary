"""ARCH-G2 (issue #154) — preview trigger tests.

Exercises :mod:`server.callbacks.preview_triggers`:

- four trigger predicates fire correctly (pre-production, scene
  complete, act complete, halfway milestone),
- triggers are idempotent — each milestone fires at most once per
  run,
- triggers do **not** advance the pipeline or mutate artifact tags.

ffmpeg work inside the builder is patched out with ``mock.patch`` —
trigger behaviour is entirely determined by state predicates, and
rendering is covered by the G1 test suite.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest import mock

import opentimelineio as otio
import pytest

from callbacks import preview_triggers
from callbacks.preview_triggers import (
    PREVIEW_LEDGER_KEY,
    act_complete_predicates,
    halfway_predicate,
    pre_production_predicate,
    pre_production_preview_after_agent_callback,
    preview_triggers_after_agent_callback,
    scene_complete_predicates,
)
from previews.builder import (
    LATEST_PREVIEW_KEY,
    PREVIEW_ARTIFACT_KIND,
    PREVIEW_HISTORY_KEY,
    PreviewManifest,
    SlotKind,
    SlotPlan,
    SlotStatus,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_gap(name: str, duration_sec: float, **doc) -> otio.schema.Gap:
    gap = otio.schema.Gap(
        name=name,
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, 24),
            duration=otio.opentime.RationalTime(duration_sec * 24, 24),
        ),
    )
    gap.metadata["documentary"] = doc
    return gap


def _make_clip(
    name: str, duration_sec: float, target_url: str, **doc
) -> otio.schema.Clip:
    # media_reference points at a non-existent path — classifier will
    # flag it as MISSING unless we pre-touch the file or override.
    clip = otio.schema.Clip(
        name=name,
        media_reference=otio.schema.ExternalReference(target_url=target_url),
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, 24),
            duration=otio.opentime.RationalTime(duration_sec * 24, 24),
        ),
    )
    clip.metadata["documentary"] = doc
    return clip


def _write_placeholder_file(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\x00")


def _build_timeline(
    tmp_path,
    *,
    scenes: list[dict],
) -> str:
    """Build an OTIO timeline with the given scene specs.

    Each scene spec is::

        {"scene_num": int,
         "video_status": "delivered"|"missing"|"failed",
         "narration_status": "delivered"|"missing",
         "duration_sec": float}
    """
    timeline = otio.schema.Timeline(name="t")
    video_track = otio.schema.Track(
        name="V1_Video", kind=otio.schema.TrackKind.Video
    )
    narr_track = otio.schema.Track(
        name="A1_Narration", kind=otio.schema.TrackKind.Audio
    )

    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    for spec in scenes:
        sn = spec["scene_num"]
        dur = spec["duration_sec"]

        # Video.
        v_status = spec["video_status"]
        if v_status == "delivered":
            vpath = str(media_dir / f"scene_{sn:03d}_V1.mp4")
            _write_placeholder_file(vpath)
            video_track.append(
                _make_clip(
                    f"scene_{sn:03d}_V1", dur, f"file://{vpath}",
                    scene_num=sn, type="body",
                )
            )
        else:
            # Both "missing" and "failed" render as gaps in OTIO here;
            # "failed" is attached through metadata.status so the
            # classifier treats it as terminal-failed.
            doc: dict[str, Any] = {"scene_num": sn, "status": "empty"}
            if v_status == "failed":
                doc["status"] = "failed"
                doc["failure_reason"] = "test-injected failure"
            video_track.append(
                _make_gap(f"scene_{sn:03d}_V1", dur, **doc)
            )

        # Narration.
        n_status = spec["narration_status"]
        if n_status == "delivered":
            npath = str(media_dir / f"scene_{sn:03d}_V1.wav")
            _write_placeholder_file(npath)
            narr_track.append(
                _make_clip(
                    f"scene_{sn:03d}_V1_audio", dur, f"file://{npath}",
                    scene_num=sn, type="narration", scripted_text=f"s{sn}",
                )
            )
        else:
            narr_track.append(
                _make_gap(
                    f"scene_{sn:03d}_V1_audio", dur, scene_num=sn,
                    status="empty",
                )
            )

    timeline.tracks.append(video_track)
    timeline.tracks.append(narr_track)
    tl_path = str(tmp_path / "timeline.otio")
    otio.adapters.write_to_file(timeline, tl_path)
    return tl_path


@pytest.fixture
def patched_builder(tmp_path):
    """Replace :func:`build_preview` with a deterministic fake.

    The fake records every call it receives in ``calls`` and returns a
    canned :class:`PreviewManifest` so the trigger can still record it
    on the blackboard.
    """
    calls: list[dict] = []

    def fake(state, trigger_reason, output_dir=None):
        calls.append({
            "trigger_reason": trigger_reason,
            "timeline_path": state.get("_timeline_path", ""),
        })
        return PreviewManifest(
            kind=PREVIEW_ARTIFACT_KIND,
            preview_path=str(tmp_path / f"preview_{trigger_reason}.mp4"),
            manifest_path=str(
                tmp_path / f"preview_{trigger_reason}.manifest.json"
            ),
            input_hash=f"hash_{trigger_reason}",
            trigger_reason=trigger_reason,
            timeline_path=state.get("_timeline_path", ""),
            otio_state="draft",
            built_at=0.0,
            total_duration_sec=10.0,
            slots=(),
            counts={},
        )

    with mock.patch.object(preview_triggers, "build_preview", side_effect=fake):
        # Also silence the dashboard SSE emit path.
        with mock.patch(
            "previews.consumers.emit_preview_ready", lambda manifest: None
        ):
            yield calls


def _ctx(state: dict) -> SimpleNamespace:
    return SimpleNamespace(state=state)


# ---------------------------------------------------------------------------
# Pre-production predicate
# ---------------------------------------------------------------------------


class TestPreProductionPredicate:

    def test_fires_when_narration_reconciliation_passed(self, tmp_path):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[{
                "scene_num": 1, "video_status": "missing",
                "narration_status": "delivered", "duration_sec": 1.0,
            }],
        )
        state = {
            "_timeline_path": tl_path,
            "pipeline_phase": "audio",
            "_narration_reconciliation_passed": True,
        }
        assert pre_production_predicate(state) is True

    def test_does_not_fire_before_reconciliation(self, tmp_path):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[{
                "scene_num": 1, "video_status": "missing",
                "narration_status": "delivered", "duration_sec": 1.0,
            }],
        )
        state = {"_timeline_path": tl_path, "pipeline_phase": "audio"}
        assert pre_production_predicate(state) is False

    def test_fires_once_then_suppressed_by_ledger(
        self, tmp_path, patched_builder
    ):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[{
                "scene_num": 1, "video_status": "missing",
                "narration_status": "delivered", "duration_sec": 1.0,
            }],
        )
        state = {
            "_timeline_path": tl_path,
            "pipeline_phase": "audio",
            "_narration_reconciliation_passed": True,
        }
        pre_production_preview_after_agent_callback(_ctx(state))
        pre_production_preview_after_agent_callback(_ctx(state))

        reasons = [c["trigger_reason"] for c in patched_builder]
        assert reasons == ["pre_production"]
        assert state[PREVIEW_LEDGER_KEY]["pre_production"] is True

    def test_non_audio_phase_is_noop(self, tmp_path, patched_builder):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[{
                "scene_num": 1, "video_status": "missing",
                "narration_status": "delivered", "duration_sec": 1.0,
            }],
        )
        state = {
            "_timeline_path": tl_path,
            "pipeline_phase": "visual_direction",
            "_narration_reconciliation_passed": True,
        }
        pre_production_preview_after_agent_callback(_ctx(state))
        assert patched_builder == []


# ---------------------------------------------------------------------------
# Scene-complete predicate
# ---------------------------------------------------------------------------


class TestScenePredicate:

    def test_detects_all_terminal(self, tmp_path):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 1.0},
                {"scene_num": 2, "video_status": "missing",
                 "narration_status": "delivered", "duration_sec": 1.0},
            ],
        )
        state = {"_timeline_path": tl_path}
        assert scene_complete_predicates(state) == {1}

    def test_failed_counts_as_terminal(self, tmp_path):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "failed",
                 "narration_status": "delivered", "duration_sec": 1.0},
            ],
        )
        state = {"_timeline_path": tl_path}
        assert scene_complete_predicates(state) == {1}

    def test_ledger_suppresses_refire(self, tmp_path, patched_builder):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 1.0},
            ],
        )
        state = {"_timeline_path": tl_path}
        preview_triggers_after_agent_callback(_ctx(state))
        preview_triggers_after_agent_callback(_ctx(state))

        scene_reasons = [
            c["trigger_reason"] for c in patched_builder
            if c["trigger_reason"].startswith("scene_")
        ]
        assert scene_reasons == ["scene_001_complete"]


# ---------------------------------------------------------------------------
# Act-complete predicate
# ---------------------------------------------------------------------------


class TestActPredicate:

    def test_fires_when_all_scenes_in_act_terminal(self, tmp_path):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 1.0},
                {"scene_num": 2, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 1.0},
            ],
        )
        state = {
            "_timeline_path": tl_path,
            "_scene_act_map": {1: 1, 2: 1},
        }
        assert act_complete_predicates(state) == {1}

    def test_does_not_fire_with_incomplete_act(self, tmp_path):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 1.0},
                {"scene_num": 2, "video_status": "missing",
                 "narration_status": "delivered", "duration_sec": 1.0},
            ],
        )
        state = {
            "_timeline_path": tl_path,
            "_scene_act_map": {1: 1, 2: 1},
        }
        assert act_complete_predicates(state) == set()

    def test_silent_without_act_map(self, tmp_path):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 1.0},
            ],
        )
        state = {"_timeline_path": tl_path}
        assert act_complete_predicates(state) == set()


# ---------------------------------------------------------------------------
# Halfway-milestone predicate
# ---------------------------------------------------------------------------


class TestHalfwayPredicate:

    def test_fires_at_50_percent(self, tmp_path):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 5.0},
                {"scene_num": 2, "video_status": "missing",
                 "narration_status": "delivered", "duration_sec": 5.0},
            ],
        )
        state = {"_timeline_path": tl_path}
        assert halfway_predicate(state) is True

    def test_does_not_fire_below_50_percent(self, tmp_path):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 1.0},
                {"scene_num": 2, "video_status": "missing",
                 "narration_status": "delivered", "duration_sec": 9.0},
            ],
        )
        state = {"_timeline_path": tl_path}
        assert halfway_predicate(state) is False

    def test_uses_scripted_durations_when_provided(self, tmp_path):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 1.0},
            ],
        )
        state = {
            "_timeline_path": tl_path,
            "_scripted_durations": {"1": 5.0, "2": 5.0},
        }
        # Scene 1 complete → 5s out of 10s scripted = 50%.
        assert halfway_predicate(state) is True

    def test_fires_once(self, tmp_path, patched_builder):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 5.0},
                {"scene_num": 2, "video_status": "missing",
                 "narration_status": "delivered", "duration_sec": 5.0},
            ],
        )
        state = {"_timeline_path": tl_path}
        preview_triggers_after_agent_callback(_ctx(state))
        preview_triggers_after_agent_callback(_ctx(state))
        halfway_calls = [
            c for c in patched_builder
            if c["trigger_reason"] == "halfway_milestone"
        ]
        assert len(halfway_calls) == 1


# ---------------------------------------------------------------------------
# Pipeline non-advancement
# ---------------------------------------------------------------------------


class TestNonAdvancement:

    def test_triggers_do_not_mutate_pipeline_gate_state(
        self, tmp_path, patched_builder
    ):
        """Triggers write ONLY preview-owned keys (LATEST_PREVIEW_KEY,
        PREVIEW_HISTORY_KEY, PREVIEW_LEDGER_KEY).  Approval gate,
        pipeline phase, and stage-ready flags must be untouched.
        """
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 5.0},
                {"scene_num": 2, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 5.0},
            ],
        )
        state: dict[str, Any] = {
            "_timeline_path": tl_path,
            "pipeline_phase": "production",
            "_narration_reconciliation_passed": True,
            "approved_stages": {"audio": True, "clips": False},
            "_stage_ready": {"clips": False},
        }
        preview_triggers_after_agent_callback(_ctx(state))

        # The preview-owned keys should be populated.
        assert LATEST_PREVIEW_KEY in state
        assert PREVIEW_HISTORY_KEY in state
        assert PREVIEW_LEDGER_KEY in state

        # The pipeline-advancing keys must be unchanged.
        assert state["pipeline_phase"] == "production"
        assert state["approved_stages"] == {"audio": True, "clips": False}
        assert state["_stage_ready"] == {"clips": False}


# ---------------------------------------------------------------------------
# Blackboard bookkeeping
# ---------------------------------------------------------------------------


class TestBlackboardKeys:

    def test_latest_and_history_written(self, tmp_path, patched_builder):
        # Scene 1 complete, but the total scripted runtime is 10s so
        # the halfway trigger does NOT fire — the only entry is the
        # scene-complete trigger.
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 1.0},
            ],
        )
        state = {
            "_timeline_path": tl_path,
            "_scripted_durations": {"1": 1.0, "2": 9.0},
        }
        preview_triggers_after_agent_callback(_ctx(state))
        assert state[LATEST_PREVIEW_KEY].endswith(
            "_scene_001_complete.mp4"
        )
        history = state[PREVIEW_HISTORY_KEY]
        assert isinstance(history, list) and len(history) == 1
        assert history[0]["kind"] == PREVIEW_ARTIFACT_KIND

    def test_multiple_triggers_append_history(self, tmp_path, patched_builder):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[
                {"scene_num": 1, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 5.0},
                {"scene_num": 2, "video_status": "delivered",
                 "narration_status": "delivered", "duration_sec": 5.0},
            ],
        )
        state = {
            "_timeline_path": tl_path,
            "_scene_act_map": {1: 1, 2: 1},
        }
        preview_triggers_after_agent_callback(_ctx(state))
        history = state[PREVIEW_HISTORY_KEY]
        # Two scene completes + one act complete + one halfway = 4.
        assert len(history) == 4
        trigger_reasons = {h["trigger_reason"] for h in history}
        assert trigger_reasons == {
            "scene_001_complete",
            "scene_002_complete",
            "act_001_complete",
            "halfway_milestone",
        }


# ---------------------------------------------------------------------------
# SlotPlan sanity — unused direct import guard
# ---------------------------------------------------------------------------


def test_slotplan_imports_ok():
    # Ensure the public symbols are stable for downstream imports.
    assert SlotKind.VIDEO.value == "video"
    assert SlotStatus.DELIVERED.value == "delivered"
    assert SlotPlan.__name__ == "SlotPlan"


# ---------------------------------------------------------------------------
# UI-06a (#208) — preview_failed emission on render failure
# ---------------------------------------------------------------------------


class TestPreviewFailedEmission:
    """When the builder raises ``PreviewRenderError`` the trigger must
    still surface the failure on the dashboard via ``preview_failed``
    — never silently degrade.
    """

    def test_render_error_emits_preview_failed(self, tmp_path):
        from previews.builder import PreviewRenderError

        tl_path = _build_timeline(
            tmp_path,
            scenes=[{
                "scene_num": 1, "video_status": "delivered",
                "narration_status": "delivered", "duration_sec": 1.0,
            }],
        )
        state = {"_timeline_path": tl_path}

        with mock.patch.object(
            preview_triggers, "build_preview",
            side_effect=PreviewRenderError("ffmpeg missing"),
        ):
            with mock.patch(
                "previews.consumers.emit_preview_failed"
            ) as mock_failed:
                preview_triggers_after_agent_callback(_ctx(state))

        assert mock_failed.call_count >= 1
        # The trigger reason must reach the dashboard verbatim so the UI
        # can normalise via derive_boundary.
        trigger_reason, error = mock_failed.call_args_list[0].args
        assert trigger_reason
        assert "ffmpeg" in error
        # The run must not have crashed — triggers swallow builder
        # failures by design.
        assert LATEST_PREVIEW_KEY not in state

    def test_unexpected_error_also_emits_preview_failed(self, tmp_path):
        tl_path = _build_timeline(
            tmp_path,
            scenes=[{
                "scene_num": 1, "video_status": "delivered",
                "narration_status": "delivered", "duration_sec": 1.0,
            }],
        )
        state = {"_timeline_path": tl_path}

        with mock.patch.object(
            preview_triggers, "build_preview",
            side_effect=RuntimeError("disk full"),
        ):
            with mock.patch(
                "previews.consumers.emit_preview_failed"
            ) as mock_failed:
                preview_triggers_after_agent_callback(_ctx(state))

        assert mock_failed.call_count >= 1
        _, error = mock_failed.call_args_list[0].args
        assert "disk full" in error
