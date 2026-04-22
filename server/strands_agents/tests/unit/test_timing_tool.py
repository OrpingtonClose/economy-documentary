"""Unit tests for the Strands timing-evaluator tool (component 02)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from strands_agents.evals.experiments.timing import (
    TIMING_EVALUATOR_THRESHOLDS,
    build_experiment,
    timing_cases,
    timing_evaluators,
)
from strands_agents.timing_tool import (
    _INTER_SCENE_PAUSE,
    _INTER_VOICE_PAUSE,
    _TIMING_TOLERANCE_MIN_SEC,
    _TIMING_TOLERANCE_PCT,
    _TIMING_TOLERANCE_SEC,
    _gap_overhead_sec,
    compute_timing_report,
    evaluate_timing,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _scene(
    scene_id: str,
    *,
    target: float = 60.0,
    voices: int = 1,
) -> dict[str, Any]:
    voice_list = [{"text": f"voice-{i}"} for i in range(voices)]
    return {
        "scene_id": scene_id,
        "scene_num": int(scene_id.split("-")[-1]) if "-" in scene_id else 0,
        "target_duration_sec": target,
        "duration_sec": target,
        "voices": voice_list,
    }


def _alignment(
    per_scene: list[tuple[str, float]],
    *,
    total: float | None = None,
) -> dict[str, Any]:
    rows = [{"scene_id": sid, "duration_sec": dur} for sid, dur in per_scene]
    total_sec = total if total is not None else sum(dur for _, dur in per_scene)
    return {"total_duration_sec": total_sec, "per_scene": rows}


# ---------------------------------------------------------------------------
# Constants are frozen — any change is a separate RFC per spec.
# ---------------------------------------------------------------------------


def test_constants_match_adk_source() -> None:
    """Tolerance and gap constants must stay byte-identical to ADK."""
    assert _TIMING_TOLERANCE_SEC == 2.0
    assert _TIMING_TOLERANCE_PCT == 0.15
    assert _TIMING_TOLERANCE_MIN_SEC == 5.0
    assert _INTER_VOICE_PAUSE == 1.5
    assert _INTER_SCENE_PAUSE == 2.5


# ---------------------------------------------------------------------------
# _gap_overhead_sec — spec lines 146-154 of timing_evaluator.py
# ---------------------------------------------------------------------------


def test_gap_overhead_no_scenes_is_zero() -> None:
    assert _gap_overhead_sec([]) == 0.0


def test_gap_overhead_single_scene_one_voice_is_zero() -> None:
    assert _gap_overhead_sec([_scene("s-1", voices=1)]) == 0.0


def test_gap_overhead_single_scene_two_active_voices_is_one_inter_voice() -> None:
    s = _scene("s-1", voices=2)
    assert _gap_overhead_sec([s]) == pytest.approx(_INTER_VOICE_PAUSE)


def test_gap_overhead_empty_voice_text_not_counted_as_active() -> None:
    s = {
        "scene_id": "s-1",
        "voices": [{"text": "line"}, {"text": ""}, {"text": "  "}],
    }
    # only one active voice → zero inter-voice gaps
    assert _gap_overhead_sec([s]) == 0.0


def test_gap_overhead_five_scenes_single_voice_each() -> None:
    scenes = [_scene(f"s-{i}", voices=1) for i in range(1, 6)]
    # 4 inter-scene gaps only
    assert _gap_overhead_sec(scenes) == pytest.approx(4 * _INTER_SCENE_PAUSE)


def test_gap_overhead_mixed_voices_and_scenes() -> None:
    scenes = [
        _scene("s-1", voices=2),  # 1 inter-voice gap
        _scene("s-2", voices=3),  # 2 inter-voice gaps
        _scene("s-3", voices=1),  # 0 inter-voice gaps
    ]
    expected = 3 * _INTER_VOICE_PAUSE + 2 * _INTER_SCENE_PAUSE
    assert _gap_overhead_sec(scenes) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# compute_timing_report — error paths
# ---------------------------------------------------------------------------


def test_missing_total_duration_raises() -> None:
    with pytest.raises(KeyError, match="total_duration_sec"):
        compute_timing_report(
            scenes=[_scene("s-1")],
            whisperx_alignment={"per_scene": []},
            target_duration_sec=60.0,
        )


def test_missing_per_scene_raises() -> None:
    with pytest.raises(KeyError, match="per_scene"):
        compute_timing_report(
            scenes=[_scene("s-1")],
            whisperx_alignment={"total_duration_sec": 0.0},
            target_duration_sec=60.0,
        )


def test_non_positive_target_without_intent_raises() -> None:
    with pytest.raises(ValueError, match="target_duration_sec must be positive"):
        compute_timing_report(
            scenes=[_scene("s-1")],
            whisperx_alignment=_alignment([("s-1", 60.0)]),
            target_duration_sec=0.0,
            intent_target_sec=None,
        )


def test_zero_intent_target_falls_back_to_legacy_path() -> None:
    """intent_target_sec=0 is treated as unset — legacy path with target."""
    scenes = [_scene("s-1", target=60.0)]
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=_alignment([("s-1", 60.0)]),
        target_duration_sec=60.0,
        intent_target_sec=0.0,
    )
    assert out["timing_report"]["mode"] == "legacy"


# ---------------------------------------------------------------------------
# Intent mode — ±2 s absolute, compared against movie runtime
# ---------------------------------------------------------------------------


def test_intent_exact_passes() -> None:
    scenes = [_scene(f"s-{i}", target=60.0) for i in range(1, 6)]
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=_alignment(
            [(f"s-{i}", 58.0) for i in range(1, 6)], total=290.0
        ),
        target_duration_sec=300.0,
        intent_target_sec=300.0,
    )
    assert out["timing_passed"] is True
    rep = out["timing_report"]
    assert rep["mode"] == "intent"
    assert rep["movie_duration_sec"] == pytest.approx(300.0)
    assert rep["deviation_sec"] == pytest.approx(0.0)
    assert rep["tolerance_sec"] == pytest.approx(2.0)
    assert rep["violations"] == []


def test_intent_within_tolerance_over_passes() -> None:
    scenes = [_scene(f"s-{i}", target=60.0) for i in range(1, 6)]
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=_alignment(
            [(f"s-{i}", 58.3) for i in range(1, 6)], total=291.5
        ),
        target_duration_sec=300.0,
        intent_target_sec=300.0,
    )
    assert out["timing_passed"] is True
    assert out["timing_report"]["deviation_sec"] == pytest.approx(1.5)


def test_intent_over_by_3s_fails() -> None:
    scenes = [_scene(f"s-{i}", target=60.0) for i in range(1, 6)]
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=_alignment(
            [(f"s-{i}", 58.6) for i in range(1, 6)], total=293.0
        ),
        target_duration_sec=300.0,
        intent_target_sec=300.0,
    )
    assert out["timing_passed"] is False
    rep = out["timing_report"]
    assert rep["deviation_sec"] == pytest.approx(3.0)
    assert rep["violations"], "expected a movie-level violation"
    assert "deviation_sec=<+3.00>" in rep["violations"][0]


def test_intent_uses_movie_runtime_not_raw_narration() -> None:
    """Movie duration = narration + gap_overhead — the intent check must
    use the former, not the latter."""
    scenes = [_scene(f"s-{i}", target=60.0) for i in range(1, 6)]
    # narration 290 + 10s gaps = 300 movie. Intent = 300. Passes only if
    # the gap overhead is added; if narration alone is compared the check
    # fails (dev = -10 > 2 tol).
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=_alignment(
            [(f"s-{i}", 58.0) for i in range(1, 6)], total=290.0
        ),
        target_duration_sec=300.0,
        intent_target_sec=300.0,
    )
    assert out["timing_passed"] is True
    assert out["timing_report"]["gap_overhead_sec"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Legacy mode — max(target*0.15, 5) s, compared against raw narration
# ---------------------------------------------------------------------------


def test_legacy_exact_passes() -> None:
    scenes = [_scene(f"s-{i}", target=60.0) for i in range(1, 6)]
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=_alignment(
            [(f"s-{i}", 60.0) for i in range(1, 6)], total=300.0
        ),
        target_duration_sec=300.0,
    )
    assert out["timing_passed"] is True
    assert out["timing_report"]["mode"] == "legacy"
    assert out["timing_report"]["tolerance_sec"] == pytest.approx(45.0)


def test_legacy_within_15pct_under_passes() -> None:
    scenes = [_scene(f"s-{i}", target=60.0) for i in range(1, 6)]
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=_alignment(
            [(f"s-{i}", 52.0) for i in range(1, 6)], total=260.0
        ),
        target_duration_sec=300.0,
    )
    assert out["timing_passed"] is True
    rep = out["timing_report"]
    assert rep["deviation_sec"] == pytest.approx(-40.0)
    assert rep["tolerance_sec"] == pytest.approx(45.0)


def test_legacy_over_by_18pct_fails() -> None:
    scenes = [_scene(f"s-{i}", target=60.0) for i in range(1, 6)]
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=_alignment(
            [(f"s-{i}", 70.8) for i in range(1, 6)], total=354.0
        ),
        target_duration_sec=300.0,
    )
    assert out["timing_passed"] is False
    rep = out["timing_report"]
    assert rep["deviation_sec"] == pytest.approx(54.0)
    assert rep["tolerance_sec"] == pytest.approx(45.0)


def test_legacy_min_floor_applies_below_33s_target() -> None:
    """Below a 33.33 s target the floor of 5 s must win over 15 %."""
    scenes = [_scene("s-1", target=10.0)]
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=_alignment([("s-1", 15.0)], total=15.0),
        target_duration_sec=10.0,
    )
    # 15 - 10 = 5.0 — exactly at the 5 s floor, still passes (<=)
    assert out["timing_report"]["tolerance_sec"] == pytest.approx(5.0)
    assert out["timing_passed"] is True


def test_legacy_ignores_gap_overhead() -> None:
    """Legacy mode compares raw narration — gap overhead must NOT shift
    the comparison even though it's still reported for observability."""
    scenes = [_scene(f"s-{i}", target=60.0) for i in range(1, 6)]
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=_alignment(
            [(f"s-{i}", 60.0) for i in range(1, 6)], total=300.0
        ),
        target_duration_sec=300.0,
    )
    rep = out["timing_report"]
    assert rep["gap_overhead_sec"] == pytest.approx(10.0)
    # legacy check is narration vs target → dev 0
    assert rep["deviation_sec"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Per-scene spike — overall-ok but a single scene out of tolerance fails
# ---------------------------------------------------------------------------


def test_per_scene_spike_fails_even_when_movie_duration_is_exact() -> None:
    scenes = [_scene(f"s-{i}", target=60.0) for i in range(1, 6)]
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=_alignment(
            [
                ("s-1", 52.0),
                ("s-2", 80.0),
                ("s-3", 53.0),
                ("s-4", 52.0),
                ("s-5", 53.0),
            ],
            total=290.0,
        ),
        target_duration_sec=300.0,
        intent_target_sec=300.0,
    )
    assert out["timing_passed"] is False
    rep = out["timing_report"]
    # movie-level check still passes — the failure comes from per-scene
    assert rep["deviation_sec"] == pytest.approx(0.0)
    per_scene = rep["per_scene_analysis"]
    assert [p["scene_id"] for p in per_scene] == ["s-1", "s-2", "s-3", "s-4", "s-5"]
    assert [p["ok"] for p in per_scene] == [True, False, True, True, True]
    assert any("s-2" in v for v in rep["violations"])


def test_per_scene_uses_percent_tolerance_not_intent_tolerance() -> None:
    """Per-scene check must always use ``max(target*0.15, 5 s)`` even in
    intent mode — the ±2 s bound is a *movie-level* invariant."""
    scenes = [_scene(f"s-{i}", target=60.0) for i in range(1, 6)]
    out = compute_timing_report(
        scenes=scenes,
        whisperx_alignment=_alignment(
            [(f"s-{i}", 58.0) for i in range(1, 6)], total=290.0
        ),
        target_duration_sec=300.0,
        intent_target_sec=300.0,
    )
    for p in out["timing_report"]["per_scene_analysis"]:
        assert p["tolerance_sec"] == pytest.approx(9.0)  # 60 * 0.15


# ---------------------------------------------------------------------------
# @tool wrapper — same-input determinism
# ---------------------------------------------------------------------------


def test_tool_wrapped_returns_same_shape() -> None:
    scenes = [_scene(f"s-{i}", target=60.0) for i in range(1, 6)]
    alignment = _alignment([(f"s-{i}", 58.0) for i in range(1, 6)], total=290.0)
    out = evaluate_timing.__wrapped__(
        scenes=scenes,
        whisperx_alignment=alignment,
        target_duration_sec=300.0,
        intent_target_sec=300.0,
    )
    assert set(out.keys()) == {"timing_passed", "timing_report"}
    assert isinstance(out["timing_passed"], bool)
    assert isinstance(out["timing_report"], dict)


def test_same_input_produces_identical_report() -> None:
    scenes = [_scene(f"s-{i}", target=60.0) for i in range(1, 6)]
    alignment = _alignment([(f"s-{i}", 58.3) for i in range(1, 6)], total=291.5)
    out_a = evaluate_timing.__wrapped__(
        scenes=copy.deepcopy(scenes),
        whisperx_alignment=copy.deepcopy(alignment),
        target_duration_sec=300.0,
        intent_target_sec=300.0,
    )
    out_b = evaluate_timing.__wrapped__(
        scenes=copy.deepcopy(scenes),
        whisperx_alignment=copy.deepcopy(alignment),
        target_duration_sec=300.0,
        intent_target_sec=300.0,
    )
    assert out_a == out_b
    # serialisable for CI artifact upload
    assert json.loads(json.dumps(out_a)) == out_a


# ---------------------------------------------------------------------------
# Experiment factory shape
# ---------------------------------------------------------------------------


def test_experiment_has_seven_cases() -> None:
    exp = build_experiment()
    cases = exp.cases
    assert len(cases) == 7
    names = [c.name for c in cases]
    assert names == [
        "intent_exact",
        "intent_within_2s_over",
        "intent_over_by_3s",
        "legacy_exact",
        "legacy_within_15pct_under",
        "legacy_over_by_18pct",
        "per_scene_spike",
    ]


def test_experiment_has_two_evaluators() -> None:
    evaluators = timing_evaluators()
    assert len(evaluators) == 2
    names = {type(e).__name__ for e in evaluators}
    assert names == {"ContractComplianceEvaluator", "Equals"}


def test_thresholds_cover_every_evaluator() -> None:
    evaluator_names = {type(e).__name__ for e in timing_evaluators()}
    assert evaluator_names == set(TIMING_EVALUATOR_THRESHOLDS.keys())


def test_every_threshold_is_hard_gate() -> None:
    for _name, (_score, hard) in TIMING_EVALUATOR_THRESHOLDS.items():
        assert hard is True


def test_cases_have_expected_output_shape() -> None:
    """Equals requires a concrete expected_output per case."""
    for case in timing_cases():
        assert case.expected_output is not None
        assert "timing_passed" in case.expected_output  # type: ignore[operator]


def test_expected_outputs_match_actual_tool_behaviour() -> None:
    """Every case's expected ``timing_passed`` must line up with what the
    real tool emits — otherwise the Equals evaluator would pass trivially."""
    for case in timing_cases():
        kwargs = case.input
        assert isinstance(kwargs, dict)
        actual = evaluate_timing.__wrapped__(**kwargs)
        assert case.expected_output is not None
        assert actual["timing_passed"] == case.expected_output["timing_passed"], (
            f"case={case.name} expected={case.expected_output} "
            f"actual={actual['timing_passed']} report={actual['timing_report']}"
        )
