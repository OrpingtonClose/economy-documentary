"""Direct-proof tests for :mod:`strands_agents.quanta.style_lock`."""

from __future__ import annotations

from typing import Any

from strands_agents.quanta import check_style_lock


_STYLE_LOCK: dict[str, Any] = {
    "positive_fragment": "clean minimal 2D infographic",
    "forbidden_styles": ["anime", "watercolor", "cyberpunk"],
}


def _concept(
    phrase_id: str,
    *,
    scene_id: int = 1,
    shot_type: str = "medium",
    camera_movement: str = "locked",
    prompt: str = "clean minimal 2D infographic showing inflation curves",
    style_lock_applied: bool = True,
) -> dict[str, Any]:
    return {
        "phrase_id": phrase_id,
        "scene_id": scene_id,
        "shot_type": shot_type,
        "camera_movement": camera_movement,
        "prompt": prompt,
        "style_lock_applied": style_lock_applied,
    }


class TestCheckStyleLock:
    def test_clean_concepts_pass(self) -> None:
        concepts = [_concept("p1"), _concept("p2", scene_id=2)]
        out = check_style_lock(concepts, _STYLE_LOCK)
        assert out == {"ok": True, "violations": []}

    def test_empty_concepts_fails_with_empty_code(self) -> None:
        out = check_style_lock([], _STYLE_LOCK)
        assert out["ok"] is False
        codes = [v["code"] for v in out["violations"]]
        assert "empty_concepts" in codes

    def test_missing_positive_fragment(self) -> None:
        concepts = [_concept("p1", prompt="some other description")]
        out = check_style_lock(concepts, _STYLE_LOCK)
        codes = {v["code"] for v in out["violations"]}
        assert "missing_positive_fragment" in codes
        assert out["ok"] is False

    def test_forbidden_style_token_in_prompt(self) -> None:
        concepts = [
            _concept("p1", prompt="clean minimal 2D infographic anime style")
        ]
        out = check_style_lock(concepts, _STYLE_LOCK)
        codes = {v["code"] for v in out["violations"]}
        assert "forbidden_style_in_prompt" in codes

    def test_bad_shot_type_reports_violation(self) -> None:
        concepts = [_concept("p1", shot_type="super_zoom")]
        out = check_style_lock(concepts, _STYLE_LOCK)
        codes = {v["code"] for v in out["violations"]}
        assert "bad_shot_type" in codes

    def test_bad_camera_movement_reports_violation(self) -> None:
        concepts = [_concept("p1", camera_movement="space_flip")]
        out = check_style_lock(concepts, _STYLE_LOCK)
        codes = {v["code"] for v in out["violations"]}
        assert "bad_camera_movement" in codes

    def test_style_lock_not_applied_reports_violation(self) -> None:
        concepts = [_concept("p1", style_lock_applied=False)]
        out = check_style_lock(concepts, _STYLE_LOCK)
        codes = {v["code"] for v in out["violations"]}
        assert "style_lock_not_applied" in codes

    def test_repeated_camera_movement_within_scene_reports(self) -> None:
        concepts = [
            _concept("p1", scene_id=1, camera_movement="dolly_in"),
            _concept("p2", scene_id=1, camera_movement="dolly_in"),
        ]
        out = check_style_lock(concepts, _STYLE_LOCK)
        codes = {v["code"] for v in out["violations"]}
        assert "repeated_camera_movement" in codes

    def test_locked_repeat_is_allowed(self) -> None:
        concepts = [
            _concept("p1", scene_id=1, camera_movement="locked"),
            _concept("p2", scene_id=1, camera_movement="locked"),
        ]
        out = check_style_lock(concepts, _STYLE_LOCK)
        assert out["ok"] is True
