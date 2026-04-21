"""Direct-proof tests for :mod:`strands_agents.quanta.coherence`."""

from __future__ import annotations

from typing import Any

from strands_agents.quanta import compute_structural_violations


_STYLE_LOCK: dict[str, Any] = {
    "positive_fragment": "clean minimal 2D infographic",
    "forbidden_styles": ["anime", "watercolor", "cyberpunk"],
}

_CONTENT_ANALYSIS: dict[str, Any] = {
    "per_scene": [
        {
            "scene_id": 1,
            "phrases": [
                {"phrase_id": "s1_p1"},
                {"phrase_id": "s1_p2"},
            ],
        },
        {
            "scene_id": 2,
            "phrases": [
                {"phrase_id": "s2_p1"},
            ],
        },
    ]
}


def _concept(
    phrase_id: str,
    *,
    shot_type: str = "medium",
    camera_movement: str = "dolly_in",
    prompt: str = "clean minimal 2D infographic of inflation trend",
) -> dict[str, Any]:
    return {
        "phrase_id": phrase_id,
        "shot_type": shot_type,
        "camera_movement": camera_movement,
        "prompt": prompt,
    }


class TestComputeStructuralViolations:
    def test_complete_coverage_passes(self) -> None:
        concepts = [
            _concept("s1_p1", shot_type="medium", camera_movement="dolly_in"),
            _concept("s1_p2", shot_type="wide", camera_movement="pan_left"),
            _concept("s2_p1", shot_type="close_up", camera_movement="locked"),
        ]
        assert compute_structural_violations(concepts, _STYLE_LOCK, _CONTENT_ANALYSIS) == []

    def test_missing_phrase_is_violation(self) -> None:
        concepts = [
            _concept("s1_p1"),
            _concept("s2_p1"),
            # s1_p2 missing
        ]
        violations = compute_structural_violations(
            concepts, _STYLE_LOCK, _CONTENT_ANALYSIS
        )
        assert any("s1_p2" in v and "missing" in v for v in violations)

    def test_duplicate_phrase_is_violation(self) -> None:
        concepts = [
            _concept("s1_p1"),
            _concept("s1_p2"),
            _concept("s1_p2"),
            _concept("s2_p1"),
        ]
        violations = compute_structural_violations(
            concepts, _STYLE_LOCK, _CONTENT_ANALYSIS
        )
        assert any("s1_p2" in v and "2 concepts" in v for v in violations)

    def test_unknown_phrase_id_is_violation(self) -> None:
        concepts = [
            _concept("s1_p1"),
            _concept("s1_p2"),
            _concept("s2_p1"),
            _concept("s99_ghost"),
        ]
        violations = compute_structural_violations(
            concepts, _STYLE_LOCK, _CONTENT_ANALYSIS
        )
        assert any("s99_ghost" in v and "not present" in v for v in violations)

    def test_forbidden_token_in_prompt_is_violation(self) -> None:
        concepts = [
            _concept("s1_p1"),
            _concept("s1_p2", prompt="anime-styled chart with gradients"),
            _concept("s2_p1"),
        ]
        violations = compute_structural_violations(
            concepts, _STYLE_LOCK, _CONTENT_ANALYSIS
        )
        assert any("forbidden" in v and "anime" in v for v in violations)

    def test_more_than_three_consecutive_identical_shots_is_violation(self) -> None:
        # 4 concepts, all same shot_type+camera_movement, one missing from
        # content analysis — focus on the consecutive-run check.
        content = {
            "per_scene": [
                {
                    "scene_id": 1,
                    "phrases": [
                        {"phrase_id": f"p{i}"} for i in range(1, 5)
                    ],
                }
            ]
        }
        concepts = [
            _concept("p1", shot_type="medium", camera_movement="locked"),
            _concept("p2", shot_type="medium", camera_movement="locked"),
            _concept("p3", shot_type="medium", camera_movement="locked"),
            _concept("p4", shot_type="medium", camera_movement="locked"),
        ]
        violations = compute_structural_violations(concepts, _STYLE_LOCK, content)
        assert any("consecutive" in v for v in violations)

    def test_three_consecutive_identical_shots_ok(self) -> None:
        content = {
            "per_scene": [
                {
                    "scene_id": 1,
                    "phrases": [{"phrase_id": f"p{i}"} for i in range(1, 4)],
                }
            ]
        }
        concepts = [
            _concept("p1", shot_type="medium", camera_movement="locked"),
            _concept("p2", shot_type="medium", camera_movement="locked"),
            _concept("p3", shot_type="medium", camera_movement="locked"),
        ]
        assert compute_structural_violations(concepts, _STYLE_LOCK, content) == []
