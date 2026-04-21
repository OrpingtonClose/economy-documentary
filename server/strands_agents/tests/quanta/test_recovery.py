"""Direct-proof tests for :mod:`strands_agents.quanta.recovery`."""

from __future__ import annotations

from typing import Any

import pytest

from strands_agents.quanta import (
    classify_failure,
    diff_concept,
    propose_revised_concept,
)


_CONCEPT: dict[str, Any] = {"scene_id": "s1", "phrase_id": "s1_p1"}


class TestClassifyFailure:
    def test_catastrophic_signal_is_catastrophic(self) -> None:
        out = classify_failure("all workers returned 500", [], _CONCEPT)
        assert out["class"] == "catastrophic"
        assert "all_workers_500" in out["signals"]

    def test_cuda_oom_is_transient(self) -> None:
        out = classify_failure("CUDA out of memory", [], _CONCEPT)
        assert out["class"] == "transient"

    def test_style_mismatch_is_fixable(self) -> None:
        out = classify_failure("output style does not match", [], _CONCEPT)
        assert out["class"] == "fixable"

    def test_fixable_repeated_twice_becomes_persistent(self) -> None:
        err = "output style does not match"
        history = [{"error": err}, {"error": err}]
        out = classify_failure(err, history, _CONCEPT)
        assert out["class"] == "persistent"

    def test_unknown_error_defaults_to_fixable(self) -> None:
        out = classify_failure("unrecognised glitch happened", [], _CONCEPT)
        assert out["class"] == "fixable"

    def test_deterministic(self) -> None:
        a = classify_failure("timeout hitting worker", [], _CONCEPT)
        b = classify_failure("timeout hitting worker", [], _CONCEPT)
        assert a == b


class TestProposeRevisedConcept:
    def test_preserves_identity_fields(self) -> None:
        original = {
            "scene_id": "s1",
            "phrase_id": "s1_p1",
            "duration_sec": 3.5,
            "prompt": "a factory",
            "negative_prompt": "",
        }
        revised = propose_revised_concept(
            original, "style drift", "style mismatch", {}
        )
        assert revised["scene_id"] == "s1"
        assert revised["phrase_id"] == "s1_p1"
        assert revised["duration_sec"] == 3.5
        assert revised["style_lock_applied"] is True

    def test_prompt_changes_meaningfully(self) -> None:
        original = {
            "scene_id": "s1",
            "phrase_id": "s1_p1",
            "prompt": "a factory",
            "negative_prompt": "",
        }
        revised = propose_revised_concept(
            original, "style drift", "style mismatch", {}
        )
        assert revised["prompt"] != original["prompt"]
        # revised negative_prompt carries the fallback negatives
        assert "deformed" in revised["negative_prompt"].lower()

    def test_style_lock_camera_movement_applied_when_missing(self) -> None:
        original = {
            "scene_id": "s1",
            "phrase_id": "s1_p1",
            "prompt": "a city",
            "negative_prompt": "",
        }
        revised = propose_revised_concept(
            original, "", "", {"camera_movement": "locked"}
        )
        assert revised["camera_movement"] == "locked"

    def test_missing_scene_id_raises(self) -> None:
        with pytest.raises(ValueError, match="scene_id"):
            propose_revised_concept({"phrase_id": "p1"}, "", "", {})

    def test_missing_phrase_id_raises(self) -> None:
        with pytest.raises(ValueError, match="phrase_id"):
            propose_revised_concept({"scene_id": "s1"}, "", "", {})

    def test_empty_concept_raises(self) -> None:
        with pytest.raises(ValueError, match="original_concept"):
            propose_revised_concept({}, "", "", {})


class TestDiffConcept:
    def test_identical_inputs_have_no_changes(self) -> None:
        d = {"a": 1, "b": 2}
        out = diff_concept(d, d)
        assert out["changed_fields"] == []
        assert out["preserved_fields"] == ["a", "b"]

    def test_value_change_reported_as_changed(self) -> None:
        out = diff_concept({"prompt": "old"}, {"prompt": "new"})
        assert out["changed_fields"] == ["prompt"]

    def test_added_key_is_changed(self) -> None:
        out = diff_concept({"a": 1}, {"a": 1, "b": 2})
        assert "b" in out["changed_fields"]
        assert "a" in out["preserved_fields"]

    def test_result_is_sorted(self) -> None:
        out = diff_concept(
            {"z": 1, "a": 2, "m": 3}, {"z": 99, "a": 2, "m": 42}
        )
        assert out["changed_fields"] == sorted(out["changed_fields"])
        assert out["preserved_fields"] == sorted(out["preserved_fields"])
