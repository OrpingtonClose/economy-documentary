"""Direct-proof tests for :mod:`strands_agents.quanta.timing`.

Mechanical in/out checks — deterministic inputs, deterministic
expected outputs. No LLM judge on the pure atom itself; judgment-of-
judgment lives at the composition-proof layer.

Tolerances exercised here mirror the constants in
:mod:`strands_agents.timing_tool`:

* legacy mode  — ``max(target * 0.15, 5.0)`` on narration total
* intent mode  — absolute ±2 s on movie runtime (narration + gaps)
* per-scene    — always the legacy percent/min floor, 5 s minimum
"""

from __future__ import annotations

from typing import Any

import pytest

from strands_agents.quanta import compute_timing_report


def _scene(scene_id: int, target: float) -> dict[str, Any]:
    return {
        "scene_id": scene_id,
        "target_duration_sec": target,
        "voices": [{"text": "the quick brown fox"}],
    }


def _alignment(total: float, per_scene: list[tuple[int, float]]) -> dict[str, Any]:
    return {
        "total_duration_sec": total,
        "per_scene": [
            {"scene_id": sid, "duration_sec": dur} for sid, dur in per_scene
        ],
    }


class TestComputeTimingReport:
    def test_passes_when_total_and_per_scene_within_tolerance(self) -> None:
        scenes = [_scene(1, 40.0), _scene(2, 60.0)]
        alignment = _alignment(100.0, [(1, 40.0), (2, 60.0)])

        out = compute_timing_report(scenes, alignment, target_duration_sec=100.0)

        assert out["timing_passed"] is True
        assert out["timing_report"]["violations"] == []
        assert out["timing_report"]["deviation_sec"] == pytest.approx(0.0)
        assert out["timing_report"]["mode"] == "legacy"

    def test_fails_when_total_exceeds_legacy_tolerance(self) -> None:
        # Target 100 s → tolerance max(15, 5) = 15 s. Overshoot by 25 s.
        scenes = [_scene(1, 40.0), _scene(2, 60.0)]
        alignment = _alignment(125.0, [(1, 50.0), (2, 75.0)])

        out = compute_timing_report(scenes, alignment, target_duration_sec=100.0)

        assert out["timing_passed"] is False
        assert any(
            "total_duration" in v for v in out["timing_report"]["violations"]
        )
        assert out["timing_report"]["deviation_sec"] == pytest.approx(25.0)

    def test_per_scene_drift_fails_even_when_total_ok(self) -> None:
        # Total lines up with target; each scene off by ±15 s — above
        # the 5 s / 15% per-scene floor for both.
        scenes = [_scene(1, 40.0), _scene(2, 60.0)]
        alignment = _alignment(100.0, [(1, 55.0), (2, 45.0)])

        out = compute_timing_report(scenes, alignment, target_duration_sec=100.0)

        assert out["timing_passed"] is False
        violations = out["timing_report"]["violations"]
        assert any("scene 1" in v for v in violations)
        assert any("scene 2" in v for v in violations)

    def test_intent_mode_uses_movie_runtime_with_two_second_tolerance(self) -> None:
        # 2 scenes, one active voice each. 1 inter-scene gap = 2.5 s.
        # Movie runtime = narration (10) + gap (2.5) = 12.5 s.
        scenes = [_scene(1, 4.0), _scene(2, 6.0)]
        alignment = _alignment(10.0, [(1, 4.0), (2, 6.0)])

        out = compute_timing_report(
            scenes,
            alignment,
            target_duration_sec=10.0,
            intent_target_sec=12.5,
        )

        report = out["timing_report"]
        assert report["mode"] == "intent"
        assert report["movie_duration_sec"] == pytest.approx(12.5)
        assert report["tolerance_sec"] == pytest.approx(2.0)
        assert out["timing_passed"] is True

    def test_intent_mode_fails_when_movie_runtime_exceeds_two_seconds(self) -> None:
        scenes = [_scene(1, 4.0), _scene(2, 6.0)]
        # Narration 14 s + 2.5 s gap = 16.5 s; intent target 10 →
        # deviation 6.5 s, way above ±2 s.
        alignment = _alignment(14.0, [(1, 6.0), (2, 8.0)])

        out = compute_timing_report(
            scenes,
            alignment,
            target_duration_sec=10.0,
            intent_target_sec=10.0,
        )

        assert out["timing_passed"] is False
        assert out["timing_report"]["mode"] == "intent"
        assert any(
            "total_duration" in v for v in out["timing_report"]["violations"]
        )

    def test_missing_alignment_keys_raise(self) -> None:
        scenes = [_scene(1, 4.0)]
        with pytest.raises(KeyError, match="total_duration_sec"):
            compute_timing_report(scenes, {"per_scene": []}, target_duration_sec=4.0)
        with pytest.raises(KeyError, match="per_scene"):
            compute_timing_report(
                scenes,
                {"total_duration_sec": 4.0},
                target_duration_sec=4.0,
            )

    def test_zero_target_without_intent_raises(self) -> None:
        scenes = [_scene(1, 4.0)]
        alignment = _alignment(4.0, [(1, 4.0)])
        with pytest.raises(ValueError, match="target_duration_sec"):
            compute_timing_report(scenes, alignment, target_duration_sec=0.0)

    def test_deterministic_same_input_same_output(self) -> None:
        scenes = [_scene(1, 40.0), _scene(2, 60.0)]
        alignment = _alignment(103.0, [(1, 41.0), (2, 62.0)])

        first = compute_timing_report(scenes, alignment, target_duration_sec=100.0)
        second = compute_timing_report(scenes, alignment, target_duration_sec=100.0)

        assert first == second
