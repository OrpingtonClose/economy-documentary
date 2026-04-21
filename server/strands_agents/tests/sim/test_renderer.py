"""Direct-proof tests for :class:`FakeRenderer`."""

from __future__ import annotations

import os

import pytest

from strands_agents.sim.recorder import Recorder
from strands_agents.sim.renderer import FakeRenderer, RenderOutcome


def _dispatch(
    r: FakeRenderer,
    *,
    scene_id: str = "s1",
    concept_id: str = "c1",
    seed: int = 1,
    duration: float = 4.0,
) -> dict:
    return r.dispatch(
        scene_id=scene_id,
        concept_id=concept_id,
        prompt="a cinematic shot of coins",
        style_lock={"tokens": ["35mm", "muted"]},
        duration_sec=duration,
        seed=seed,
        audio_artifact_url="fake-b2://abc/narration.wav",
    )


class TestFakeRendererDispatch:
    def test_clean_default_writes_sentinel(self, tmp_path) -> None:
        r = FakeRenderer(tmpdir=str(tmp_path))
        out = _dispatch(r)
        assert os.path.exists(out["artifact_path"])
        assert out["black_frame_fraction"] == 0.0
        assert out["frames"] == 4 * 24  # 4 seconds @ 24fps
        assert out["codec"] == "h264"
        assert out["style_lock_tokens"] == ["35mm", "muted"]

    def test_frozen_frames_flagged(self, tmp_path) -> None:
        r = FakeRenderer(tmpdir=str(tmp_path))
        r.set_outcome(
            scene_id="s2", outcomes=RenderOutcome(kind="frozen_frames")
        )
        out = _dispatch(r, scene_id="s2")
        assert out["frozen_frame_runs"]
        assert out["black_frame_fraction"] == 0.0

    def test_black_frames_fraction_above_threshold(self, tmp_path) -> None:
        r = FakeRenderer(tmpdir=str(tmp_path))
        r.set_outcome(
            scene_id="s3", outcomes=RenderOutcome(kind="black_frames")
        )
        out = _dispatch(r, scene_id="s3")
        assert out["black_frame_fraction"] >= 0.3

    def test_wrong_duration_reported(self, tmp_path) -> None:
        r = FakeRenderer(tmpdir=str(tmp_path))
        r.set_outcome(
            scene_id="s4",
            outcomes=RenderOutcome(kind="wrong_duration", actual_duration_sec=2.1),
        )
        out = _dispatch(r, scene_id="s4", duration=4.0)
        assert out["duration_sec"] == pytest.approx(2.1)
        assert out["frames"] == pytest.approx(2.1 * 24, rel=0.05)

    def test_dispatch_error_raises(self, tmp_path) -> None:
        r = FakeRenderer(tmpdir=str(tmp_path))
        r.set_outcome(
            scene_id="s5",
            outcomes=RenderOutcome(
                kind="dispatch_error", error_message="CUDA OOM"
            ),
        )
        with pytest.raises(RuntimeError, match="CUDA OOM"):
            _dispatch(r, scene_id="s5")

    def test_outcome_queue_consumed_in_order(self, tmp_path) -> None:
        r = FakeRenderer(tmpdir=str(tmp_path))
        r.set_outcome(
            scene_id="s6",
            outcomes=[
                RenderOutcome(kind="frozen_frames"),
                RenderOutcome(kind="clean"),
            ],
        )
        first = _dispatch(r, scene_id="s6")
        second = _dispatch(r, scene_id="s6")
        assert "frozen_frame_runs" in first
        assert "frozen_frame_runs" not in second

    def test_queue_exhausted_falls_back_to_default(self, tmp_path) -> None:
        r = FakeRenderer(tmpdir=str(tmp_path))
        r.set_default_outcome(RenderOutcome(kind="black_frames"))
        r.set_outcome(
            scene_id="s7", outcomes=[RenderOutcome(kind="clean")]
        )
        first = _dispatch(r, scene_id="s7")
        second = _dispatch(r, scene_id="s7")
        assert first["black_frame_fraction"] == 0.0
        assert second["black_frame_fraction"] >= 0.3

    def test_seed_differences_give_different_artifact_paths(self, tmp_path) -> None:
        r = FakeRenderer(tmpdir=str(tmp_path))
        a = _dispatch(r, seed=1)
        b = _dispatch(r, seed=2)
        assert a["artifact_path"] != b["artifact_path"]


class TestFakeRendererHealth:
    def test_reports_worker_count(self) -> None:
        r = FakeRenderer(workers_total=3)
        snap = r.health_check()
        assert snap["workers_total"] == 3
        assert snap["workers_available"] == 3
        assert snap["queue_depth"] == 0
        assert len(snap["per_worker"]) == 3

    def test_set_health_updates_next_snapshot(self) -> None:
        r = FakeRenderer(workers_total=2)
        r.set_health(workers_available=1, queue_depth=4)
        snap = r.health_check()
        assert snap["workers_available"] == 1
        assert snap["queue_depth"] == 4

    def test_scripted_error_raises_once(self) -> None:
        r = FakeRenderer(workers_total=2)
        r.set_health(error="fleet offline")
        with pytest.raises(RuntimeError, match="fleet offline"):
            r.health_check()
        # Error clears after firing — next call succeeds so tests can
        # simulate "failed once, then recovered".
        snap = r.health_check()
        assert snap["workers_total"] == 2


class TestFakeRendererRecording:
    def test_records_dispatch_and_health(self, tmp_path) -> None:
        rec = Recorder()
        r = FakeRenderer(tmpdir=str(tmp_path), recorder=rec)
        _dispatch(r)
        r.health_check()
        ops = rec.ops(channel="renderer")
        assert ops == ["dispatch", "health_check"]

    def test_records_dispatch_errors(self, tmp_path) -> None:
        rec = Recorder()
        r = FakeRenderer(tmpdir=str(tmp_path), recorder=rec)
        r.set_outcome(
            scene_id="s1",
            outcomes=RenderOutcome(kind="dispatch_error", error_message="x"),
        )
        with pytest.raises(RuntimeError):
            _dispatch(r, scene_id="s1")
        # Failed dispatches are still recorded — trajectory tests need
        # to see retries and recoveries.
        assert rec.ops(channel="renderer") == ["dispatch"]
