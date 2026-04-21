"""Direct-proof tests for :mod:`strands_agents.quanta.scenario`."""

from __future__ import annotations

from typing import Any

from strands_agents.quanta import (
    derive_scenario_topic,
    evaluate_scenario_structural,
    sum_scenario_duration,
)


def _scene(
    scene_id: int,
    duration: float,
    *,
    title: str = "",
    visual_notes: str = "clean minimal 2D infographic",
    voices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": scene_id,
        "scene_num": scene_id,
        "title": title,
        "duration_sec": duration,
        "target_duration_sec": duration,
        "voices": voices or [{"voice_id": "narrator", "text": "hello world " * 40}],
        "visual_notes": visual_notes,
        "pronunciation_hints": {},
        "hook_spec": None,
        "outro_spec": None,
    }


_STYLE_LOCK: dict[str, Any] = {
    "dominant_style": "clean 2D infographic",
    "positive_fragment": "clean minimal 2D infographic motion graphics",
    "forbidden_styles": ["anime", "watercolor", "cyberpunk"],
}


class TestSumScenarioDuration:
    def test_sums_duration_sec(self) -> None:
        scenes = [_scene(1, 4.0), _scene(2, 6.5), _scene(3, 10.0)]
        assert sum_scenario_duration(scenes) == 20.5

    def test_empty_is_zero(self) -> None:
        assert sum_scenario_duration([]) == 0.0

    def test_falls_back_to_duration_key(self) -> None:
        scenes = [{"duration": 3.0}, {"duration_sec": 5.0}]
        assert sum_scenario_duration(scenes) == 8.0

    def test_skips_non_dict_and_non_numeric(self) -> None:
        scenes: list[Any] = [_scene(1, 5.0), "bogus", {"duration_sec": "NaN-ish"}]
        assert sum_scenario_duration(scenes) == 5.0


class TestDeriveScenarioTopic:
    def test_uses_first_title(self) -> None:
        assert derive_scenario_topic([_scene(1, 4.0, title="Hyperinflation")]) == (
            "Hyperinflation"
        )

    def test_truncates_at_60_chars(self) -> None:
        long_title = "A" * 120
        assert len(derive_scenario_topic([_scene(1, 4.0, title=long_title)])) == 60

    def test_falls_back_to_documentary(self) -> None:
        assert derive_scenario_topic([_scene(1, 4.0, title="")]) == "documentary"


class TestEvaluateScenarioStructural:
    def test_returns_rating_issues_suggestions(self) -> None:
        scenes = [_scene(i, 45.0, title=f"Scene {i}") for i in range(1, 11)]
        out = evaluate_scenario_structural(scenes, _STYLE_LOCK, target_duration_sec=420.0)

        assert out["rating"] in {"EXCELLENT", "GOOD", "FAIR", "POOR"}
        assert isinstance(out["issues"], list)
        assert isinstance(out["suggestions"], list)

    def test_duration_shortfall_is_poor(self) -> None:
        # User asked for 420 s (7 min), scenario only 230 s (3:50) — PAG bug.
        scenes = [_scene(i, 45.0, title=f"Scene {i}") for i in range(1, 6)] + [
            _scene(6, 5.0, title="Scene 6")
        ]
        out = evaluate_scenario_structural(scenes, _STYLE_LOCK, target_duration_sec=420.0)

        assert out["rating"] == "POOR"
        names = {issue["name"] for issue in out["issues"]}
        assert "duration_compliance" in names

    def test_style_violation_is_poor(self) -> None:
        scenes = [_scene(i, 45.0, title=f"Scene {i}") for i in range(1, 11)]
        scenes[3]["visual_notes"] = "cyberpunk neon cityscape"
        out = evaluate_scenario_structural(scenes, _STYLE_LOCK, target_duration_sec=420.0)

        assert out["rating"] == "POOR"
        assert any(issue["name"] == "style_consistency" for issue in out["issues"])

    def test_deterministic(self) -> None:
        scenes = [_scene(i, 45.0, title=f"Scene {i}") for i in range(1, 11)]
        first = evaluate_scenario_structural(scenes, _STYLE_LOCK, target_duration_sec=420.0)
        second = evaluate_scenario_structural(scenes, _STYLE_LOCK, target_duration_sec=420.0)
        assert first == second
