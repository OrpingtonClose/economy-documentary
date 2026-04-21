"""Direct-proof tests for :mod:`strands_agents.quanta.refiner`."""

from __future__ import annotations

from typing import Any

import pytest

from strands_agents.quanta import (
    adjust_scene_durations,
    validate_pronunciation_hints,
)


def _scene(scene_id: int, target: float, hints: dict | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": scene_id,
        "target_duration_sec": target,
        "voices": [{"voice_id": "narrator", "text": "hello"}],
    }
    if hints is not None:
        out["pronunciation_hints"] = hints
    return out


class TestAdjustSceneDurations:
    def test_updates_listed_scenes_and_leaves_others(self) -> None:
        scenes = [
            _scene(1, 4.0, hints={"PAG": "P A G"}),
            _scene(2, 6.0, hints={}),
            _scene(3, 5.0, hints={}),
        ]
        out = adjust_scene_durations(scenes, {1: 5.5, 3: 4.0})

        durations = {s["id"]: s["target_duration_sec"] for s in out["scenes"]}
        assert durations == {1: 5.5, 2: 6.0, 3: 4.0}
        assert out["updated_scene_ids"] == [1, 3]

    def test_preserves_pronunciation_hints(self) -> None:
        scenes = [_scene(1, 4.0, hints={"PAG": "P A G"})]
        out = adjust_scene_durations(scenes, {1: 5.0})
        assert out["scenes"][0]["pronunciation_hints"] == {"PAG": "P A G"}

    def test_preserves_voice_assignments(self) -> None:
        scenes = [_scene(1, 4.0, hints={})]
        out = adjust_scene_durations(scenes, {1: 5.0})
        assert out["scenes"][0]["voices"] == [
            {"voice_id": "narrator", "text": "hello"}
        ]

    def test_empty_scenes_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            adjust_scene_durations([], {1: 4.0})

    def test_non_positive_target_raises(self) -> None:
        scenes = [_scene(1, 4.0)]
        with pytest.raises(ValueError, match="positive"):
            adjust_scene_durations(scenes, {1: 0.0})
        with pytest.raises(ValueError, match="positive"):
            adjust_scene_durations(scenes, {1: -2.0})

    def test_does_not_mutate_input(self) -> None:
        scenes = [_scene(1, 4.0, hints={"PAG": "P A G"})]
        original_target = scenes[0]["target_duration_sec"]
        adjust_scene_durations(scenes, {1: 9.0})
        assert scenes[0]["target_duration_sec"] == original_target


class TestValidatePronunciationHints:
    def test_all_scenes_have_hints_passes(self) -> None:
        scenes = [_scene(1, 4.0, hints={}), _scene(2, 6.0, hints={"PAG": "P A G"})]
        out = validate_pronunciation_hints(scenes)
        assert out == {"ok": True, "missing_on": []}

    def test_reports_each_scene_missing_hints(self) -> None:
        scenes = [
            _scene(1, 4.0, hints={}),
            _scene(2, 6.0),  # no hints
            _scene(3, 5.0),  # no hints
        ]
        out = validate_pronunciation_hints(scenes)
        assert out["ok"] is False
        assert set(out["missing_on"]) == {2, 3}

    def test_empty_hints_dict_still_passes(self) -> None:
        # Hints dict present but empty → still counts as "retained".
        scenes = [_scene(1, 4.0, hints={})]
        out = validate_pronunciation_hints(scenes)
        assert out == {"ok": True, "missing_on": []}
