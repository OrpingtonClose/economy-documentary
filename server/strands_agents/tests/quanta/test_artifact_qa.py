"""Direct-proof tests for :mod:`strands_agents.quanta.artifact_qa`."""

from __future__ import annotations

from typing import Any

import pytest

from strands_agents.quanta import evaluate_visual_artifact_quality
from strands_agents.quanta.artifact_qa import (
    DEFAULT_FPS,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_WARN,
)


def _artifact(
    *,
    frames: int = int(5.0 * DEFAULT_FPS),
    duration_sec: float = 5.0,
    codec: str = "h264",
    black_frame_fraction: float = 0.0,
    artifact_path: str = "/tmp/test_clip.mp4",
) -> dict[str, Any]:
    return {
        "artifact_path": artifact_path,
        "frames": frames,
        "duration_sec": duration_sec,
        "codec": codec,
        "black_frame_fraction": black_frame_fraction,
    }


class TestEvaluateVisualArtifactQuality:
    def test_clean_clip_passes(self) -> None:
        out = evaluate_visual_artifact_quality(_artifact(), target_duration_sec=5.0)
        assert out["verdict"] == VERDICT_PASS
        assert out["passed"] is True
        assert out["issues"] == []
        assert set(out["checks"]) == {"frame_count", "duration", "codec", "black_frames"}

    def test_frame_count_mismatch_fails(self) -> None:
        out = evaluate_visual_artifact_quality(
            _artifact(frames=60), target_duration_sec=5.0
        )
        assert out["verdict"] == VERDICT_FAIL
        codes = {i["code"] for i in out["issues"]}
        assert "frame_count_mismatch" in codes

    def test_duration_mismatch_fails(self) -> None:
        out = evaluate_visual_artifact_quality(
            _artifact(duration_sec=6.0), target_duration_sec=5.0
        )
        assert out["verdict"] == VERDICT_FAIL
        codes = {i["code"] for i in out["issues"]}
        assert "duration_mismatch" in codes

    def test_unsupported_codec_fails(self) -> None:
        out = evaluate_visual_artifact_quality(
            _artifact(codec="vp9"), target_duration_sec=5.0
        )
        assert out["verdict"] == VERDICT_FAIL
        codes = {i["code"] for i in out["issues"]}
        assert "codec_unsupported" in codes

    def test_black_frame_fraction_above_ceiling_fails(self) -> None:
        out = evaluate_visual_artifact_quality(
            _artifact(black_frame_fraction=0.1), target_duration_sec=5.0
        )
        assert out["verdict"] == VERDICT_FAIL
        codes = {i["code"] for i in out["issues"]}
        assert "black_frame_ceiling_exceeded" in codes

    def test_black_frame_fraction_between_warn_and_ceiling_warns(self) -> None:
        out = evaluate_visual_artifact_quality(
            _artifact(black_frame_fraction=0.035), target_duration_sec=5.0
        )
        assert out["verdict"] == VERDICT_WARN
        assert out["passed"] is False
        codes = {i["code"] for i in out["issues"]}
        assert "black_frame_warning" in codes

    def test_non_positive_target_raises(self) -> None:
        with pytest.raises(ValueError):
            evaluate_visual_artifact_quality(_artifact(), target_duration_sec=0.0)

    def test_bad_artifact_path_raises(self) -> None:
        bad = _artifact(artifact_path="")
        with pytest.raises(ValueError):
            evaluate_visual_artifact_quality(bad, target_duration_sec=5.0)

    def test_allowed_codecs_case_insensitive(self) -> None:
        out = evaluate_visual_artifact_quality(
            _artifact(codec="H264"), target_duration_sec=5.0
        )
        assert out["verdict"] == VERDICT_PASS

    def test_deterministic(self) -> None:
        a = _artifact(duration_sec=6.0)
        first = evaluate_visual_artifact_quality(a, target_duration_sec=5.0)
        second = evaluate_visual_artifact_quality(a, target_duration_sec=5.0)
        assert first == second
