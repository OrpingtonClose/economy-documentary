"""Live-judge proof of robustness for Component 12 (recovery-agents).

Clear-cut contracts proved here:

1. Deterministic classifier: blatantly canonical errors map to the
   correct recovery class every time.
   * CUDA OOM → transient
   * "output style does not match" → fixable
   * All-workers-500 → catastrophic
   * Same fixable error repeating past budget → persistent
2. Deterministic remanifester: preserves required invariant fields
   (``phrase_id``, ``scene_id``, ``duration_sec``, ``style_lock_applied``)
   and produces at least one meaningful change to the prompt.
3. Live: Claude confirms that a remanifested prompt actually addresses
   the hint — a style-drift complaint produces a cinematically richer
   prompt, not an unrelated rewrite.
"""

from __future__ import annotations

from typing import Any

import pytest

from strands_agents.subagents.recovery_agents import (
    classify,
    diff_concept,
    persist_classification,
    propose_revised_concept,
)

from .._judges import judge_text_yes
from ..conftest import requires_google_api


# ---------------------------------------------------------------------------
# Canonical clear-cut classifications
# ---------------------------------------------------------------------------


def test_cuda_oom_error_classified_as_transient() -> None:
    result = classify.__wrapped__(
        error="RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB",
        recent_history=[],
        concept={"scene_id": "s-1", "phrase_id": "p-1"},
    )
    assert result["class"] == "transient"
    assert "cuda_oom" in result["signals"]


def test_timeout_error_classified_as_transient() -> None:
    result = classify.__wrapped__(
        error="worker call timed out after 180s",
        recent_history=[],
        concept={"scene_id": "s-1", "phrase_id": "p-1"},
    )
    assert result["class"] == "transient"
    assert "timeout" in result["signals"]


def test_style_mismatch_error_classified_as_fixable() -> None:
    result = classify.__wrapped__(
        error="QA rejected: output style does not match documentary style lock",
        recent_history=[],
        concept={"scene_id": "s-1", "phrase_id": "p-1"},
    )
    assert result["class"] == "fixable"
    # At least one of the fixable signals must fire.
    assert {"style_mismatch", "qa_rejected"} & set(result["signals"])


def test_all_workers_500_classified_as_catastrophic() -> None:
    result = classify.__wrapped__(
        error="all workers returned 500 after 3 attempts; pool exhausted",
        recent_history=[],
        concept={"scene_id": "s-1", "phrase_id": "p-1"},
    )
    assert result["class"] == "catastrophic"
    assert "all_workers_500" in result["signals"]


def test_disk_full_classified_as_catastrophic() -> None:
    result = classify.__wrapped__(
        error="IOError: No space left on device",
        recent_history=[],
        concept={"scene_id": "s-1", "phrase_id": "p-1"},
    )
    assert result["class"] == "catastrophic"
    assert "disk_full" in result["signals"]


def test_repeated_fixable_error_escalates_to_persistent() -> None:
    needle = "QA rejected: wrong subject"
    history: list[dict[str, Any]] = [{"error": needle} for _ in range(3)]
    result = classify.__wrapped__(
        error=needle,
        recent_history=history,
        concept={"scene_id": "s-1", "phrase_id": "p-1"},
    )
    assert result["class"] == "persistent"
    assert result["repeat_count"] >= 3


def test_persist_classification_rejects_unknown_class() -> None:
    with pytest.raises(ValueError, match="unknown classification"):
        persist_classification.__wrapped__(
            artifact_id="art-1",
            classification={"class": "unknown_garbage"},
        )


def test_persist_classification_records_valid_class() -> None:
    payload = persist_classification.__wrapped__(
        artifact_id="art-1",
        classification={"class": "fixable", "hint": "style drift"},
    )
    assert payload["persisted"] is True
    assert payload["classification"]["class"] == "fixable"


# ---------------------------------------------------------------------------
# Deterministic remanifestation
# ---------------------------------------------------------------------------


_ORIGINAL_CONCEPT: dict[str, Any] = {
    "phrase_id": "p-42",
    "scene_id": "s-3",
    "duration_sec": 6.0,
    "prompt": "A German family carrying banknotes to the bakery in 1923",
    "negative_prompt": "",
    "style_lock_applied": True,
    "camera_movement": "slow dolly in",
}

_STYLE_LOCK: dict[str, Any] = {
    "positive_fragment": "sepia 16mm archival footage",
    "forbidden_styles": ["anime", "cyberpunk"],
    "camera_movement": "handheld",
}


def test_remanifestation_preserves_required_fields() -> None:
    revised = propose_revised_concept.__wrapped__(
        original_concept=_ORIGINAL_CONCEPT,
        error="output style does not match",
        hint="cinematic style drift",
        style_lock=_STYLE_LOCK,
    )
    for key in ("phrase_id", "scene_id", "duration_sec"):
        assert revised[key] == _ORIGINAL_CONCEPT[key]
    assert revised["style_lock_applied"] is True


def test_remanifestation_emits_meaningful_change() -> None:
    revised = propose_revised_concept.__wrapped__(
        original_concept=_ORIGINAL_CONCEPT,
        error="style mismatch",
        hint="style drift - cinematic documentary required",
        style_lock=_STYLE_LOCK,
    )
    changes = diff_concept.__wrapped__(original=_ORIGINAL_CONCEPT, revised=revised)
    assert "prompt" in changes["changed_fields"] or (
        "negative_prompt" in changes["changed_fields"]
    )


# ---------------------------------------------------------------------------
# Live: Claude confirms revised prompt addresses the hint
# ---------------------------------------------------------------------------


@requires_google_api
def test_live_claude_confirms_style_drift_fix_addresses_hint() -> None:
    hint = "style drift - documentary should feel cinematic, film-grain, 35mm"
    revised = propose_revised_concept.__wrapped__(
        original_concept=_ORIGINAL_CONCEPT,
        error="QA rejected: output style does not match style lock",
        hint=hint,
        style_lock=_STYLE_LOCK,
    )
    verdict = judge_text_yes(
        "You are reviewing a visual concept that was revised to fix a "
        "style-drift complaint.  Does the revised prompt explicitly "
        "introduce cinematic documentary language (grain, 35mm, "
        "film-look, or similar) that would address the complaint? "
        "Answer with a single word: yes or no.\n\n"
        f"Original prompt:\n{_ORIGINAL_CONCEPT['prompt']}\n\n"
        f"Hint: {hint}\n\n"
        f"Revised prompt:\n{revised['prompt']}"
    )
    assert not verdict.disabled, f"judge disabled: {verdict.error}"
    assert verdict.is_yes, (
        f"Claude thinks revision fails to address style-drift hint: "
        f"{verdict.answer!r}. revised prompt: {revised['prompt']!r}"
    )
