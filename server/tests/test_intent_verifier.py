"""Unit tests for per-stage R0 verification (INTENT-04, #268).

Covers the invariants declared in :mod:`server.callbacks.intent_verifier`:

1. :func:`verify_stage_constraints` is a pure function — it never
   raises on drift; callers check ``record.passed``.
2. Each stage checks the subset of R0 constraints it owns:
   scenario (duration + topics), audio (measured narration duration),
   visual (aspect-ratio + topic coverage), production (per-clip
   duration + aspect-ratio), assembly (final-film duration).
3. :func:`log_verification` appends records to
   :data:`VERIFICATION_LOG_KEY` on the blackboard.
4. Unknown stages raise :class:`ValueError` so mis-wired callers fail
   loudly.
5. When no :class:`BriefIntent` is present the verifier returns a
   passed record with a ``no_brief_intent`` marker — absence of R0
   is not treated as drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from agents.intent_extractor import BriefIntent, BRIEF_INTENT_KEY  # noqa: E402
from callbacks.intent_verifier import (  # noqa: E402
    STAGE_ASSEMBLY,
    STAGE_AUDIO,
    STAGE_PRODUCTION,
    STAGE_SCENARIO,
    STAGE_VISUAL,
    VERIFICATION_LOG_KEY,
    VerificationRecord,
    log_verification,
    verify_and_log,
    verify_stage_constraints,
)


def _intent(**overrides) -> BriefIntent:
    base = dict(
        duration_sec=420.0,
        tolerance_sec=30.0,
        audience="adhd-friendly",
        tone=["curious"],
        corpus_paths=[],
        required_topics=["PAG", "opioid chemistry"],
        forbidden_topics=["recreational drug use"],
        format_hints={"aspect_ratio": "16:9"},
        confidence={"duration_sec": 0.98},
    )
    base.update(overrides)
    return BriefIntent(**base)


def _state(intent: BriefIntent, **overrides) -> dict:
    base = {BRIEF_INTENT_KEY: intent.to_json()}
    base.update(overrides)
    return base


def _scene(duration_sec: float, *, text: str = "") -> dict:
    return {
        "scene_num": 0,
        "title": "scene",
        "duration_sec": duration_sec,
        "narration": text,
    }


# ---------------------------------------------------------------------------
# Scenario stage
# ---------------------------------------------------------------------------


def test_scenario_passes_within_tolerance():
    intent = _intent()
    scenes = [
        _scene(210, text="PAG overview"),
        _scene(210, text="opioid chemistry in detail"),
    ]
    state = _state(intent, scenes=scenes)
    record = verify_stage_constraints(STAGE_SCENARIO, state)
    assert record.passed is True
    assert record.metrics["total_scene_duration_sec"] == pytest.approx(420.0)


def test_scenario_fails_on_duration_drift():
    intent = _intent()
    scenes = [_scene(100, text="PAG opioid chemistry")]
    state = _state(intent, scenes=scenes)
    record = verify_stage_constraints(STAGE_SCENARIO, state)
    assert record.passed is False


def test_scenario_fails_on_missing_required_topic():
    intent = _intent()
    scenes = [_scene(420, text="only PAG here nothing else")]
    state = _state(intent, scenes=scenes)
    record = verify_stage_constraints(STAGE_SCENARIO, state)
    assert record.passed is False
    assert "opioid chemistry" in record.metrics["missing_required_topics"]


def test_scenario_fails_on_forbidden_topic():
    intent = _intent()
    scenes = [_scene(
        420,
        text=(
            "PAG and opioid chemistry are covered alongside "
            "recreational drug use stories."
        ),
    )]
    state = _state(intent, scenes=scenes)
    record = verify_stage_constraints(STAGE_SCENARIO, state)
    assert record.passed is False
    assert "recreational drug use" in record.metrics["present_forbidden_topics"]


# ---------------------------------------------------------------------------
# Audio stage
# ---------------------------------------------------------------------------


def test_audio_uses_declared_when_no_measurement():
    intent = _intent()
    scenes = [_scene(210), _scene(210)]
    state = _state(intent, scenes=scenes)
    record = verify_stage_constraints(STAGE_AUDIO, state)
    assert record.passed is True
    assert record.metrics["measured_duration_sec"] is None


def test_audio_uses_measured_duration_when_present():
    intent = _intent()
    scenes = [_scene(210), _scene(210)]
    state = _state(
        intent,
        scenes=scenes,
        whisperx_alignment=json.dumps({"total_duration_sec": 420.0}),
    )
    record = verify_stage_constraints(STAGE_AUDIO, state)
    assert record.passed is True
    assert record.metrics["measured_duration_sec"] == pytest.approx(420.0)


def test_audio_fails_on_measured_drift_beyond_tolerance():
    intent = _intent()
    scenes = [_scene(210), _scene(210)]
    state = _state(
        intent,
        scenes=scenes,
        whisperx_alignment=json.dumps({"total_duration_sec": 500.0}),
    )
    record = verify_stage_constraints(STAGE_AUDIO, state)
    assert record.passed is False


def test_audio_passes_when_narration_plus_gaps_equals_target():
    """Regression for #282 bot review: after deterministic_steps scales
    scene durations so narration+gaps=target, the audio verifier must
    compare MOVIE runtime (not raw narration) against the user's
    target.  Pre-fix this would FAIL because 356.5s < (420 - 30)s.
    """
    intent = _intent()
    # 12 scenes × 3 active voices each.  Voice gaps: 12×2×1.5=36s.
    # Scene gaps: 11×2.5=27.5s.  Total gaps: 63.5s.  Narration
    # scaled to 420 - 63.5 = 356.5s — movie = 420s exactly.
    scenes = [
        {
            "scene_num": i,
            "title": f"s{i}",
            "duration_sec": 356.5 / 12,
            "voices": [
                {"voice": "V1", "text": "PAG"},
                {"voice": "V2", "text": "opioid chemistry"},
                {"voice": "V3", "text": "panic freeze"},
            ],
        }
        for i in range(1, 13)
    ]
    state = _state(
        intent,
        scenes=scenes,
        whisperx_alignment=json.dumps({"total_duration_sec": 356.5}),
    )
    record = verify_stage_constraints(STAGE_AUDIO, state)
    assert record.passed is True, record.failures
    assert record.metrics["movie_duration_sec"] == pytest.approx(420.0, abs=0.5)
    assert record.metrics["gap_overhead_sec"] == pytest.approx(63.5, abs=0.1)


# ---------------------------------------------------------------------------
# Visual stage
# ---------------------------------------------------------------------------


def test_visual_fails_on_missing_topic_coverage():
    intent = _intent()
    state = _state(intent, visual_concepts="general brain imagery only")
    record = verify_stage_constraints(STAGE_VISUAL, state)
    assert record.passed is False
    assert "PAG" in record.metrics["missing_required_topics"]


def test_visual_fails_on_aspect_ratio_mismatch():
    intent = _intent()
    state = _state(
        intent,
        visual_concepts={
            "aspect_ratio": "9:16",
            "notes": "PAG and opioid chemistry coverage",
        },
    )
    record = verify_stage_constraints(STAGE_VISUAL, state)
    assert record.passed is False


# ---------------------------------------------------------------------------
# Production stage
# ---------------------------------------------------------------------------


def test_production_passes_with_healthy_clips():
    intent = _intent()
    state = _state(
        intent,
        clips=[
            {"id": "a", "aspect_ratio": "16:9", "duration_sec": 6.0},
            {"id": "b", "aspect_ratio": "16:9", "duration_sec": 5.0},
        ],
    )
    record = verify_stage_constraints(STAGE_PRODUCTION, state)
    assert record.passed is True


def test_production_fails_on_zero_length_clip():
    intent = _intent()
    state = _state(
        intent,
        clips=[
            {"id": "a", "aspect_ratio": "16:9", "duration_sec": 6.0},
            {"id": "b", "aspect_ratio": "16:9", "duration_sec": 0.0},
        ],
    )
    record = verify_stage_constraints(STAGE_PRODUCTION, state)
    assert record.passed is False


def test_production_fails_on_aspect_ratio_mismatch():
    intent = _intent()
    state = _state(
        intent,
        clips=[
            {"id": "a", "aspect_ratio": "9:16", "duration_sec": 6.0},
        ],
    )
    record = verify_stage_constraints(STAGE_PRODUCTION, state)
    assert record.passed is False


# ---------------------------------------------------------------------------
# Assembly stage
# ---------------------------------------------------------------------------


def test_assembly_passes_within_tolerance():
    intent = _intent()
    state = _state(intent, final_duration_sec=430.0)
    record = verify_stage_constraints(STAGE_ASSEMBLY, state)
    assert record.passed is True


def test_assembly_fails_outside_tolerance():
    intent = _intent()
    state = _state(intent, final_duration_sec=500.0)
    record = verify_stage_constraints(STAGE_ASSEMBLY, state)
    assert record.passed is False


def test_assembly_falls_back_to_measured_narration():
    intent = _intent()
    state = _state(
        intent,
        whisperx_alignment=json.dumps({"total_duration_sec": 420.0}),
    )
    record = verify_stage_constraints(STAGE_ASSEMBLY, state)
    assert record.passed is True


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------


def test_unknown_stage_raises_value_error():
    with pytest.raises(ValueError):
        verify_stage_constraints("bogus", {})


def test_missing_intent_returns_passed_record():
    record = verify_stage_constraints(STAGE_SCENARIO, {})
    assert record.passed is True
    assert record.metrics.get("no_brief_intent") is True


def test_log_verification_appends_to_state():
    state: dict = {}
    record = VerificationRecord(stage=STAGE_SCENARIO, passed=True)
    log_verification(record, state)
    log_verification(record, state)
    entries = json.loads(state[VERIFICATION_LOG_KEY])
    assert len(entries) == 2
    assert entries[0]["stage"] == STAGE_SCENARIO


def test_log_verification_without_state_is_safe():
    record = VerificationRecord(stage=STAGE_SCENARIO, passed=False, failures=["x"])
    log_verification(record, None)  # must not raise


def test_verify_and_log_combines_both_paths():
    intent = _intent()
    scenes = [
        _scene(210, text="PAG overview"),
        _scene(210, text="opioid chemistry in detail"),
    ]
    state = _state(intent, scenes=scenes)
    record = verify_and_log(STAGE_SCENARIO, state)
    assert record.passed is True
    assert VERIFICATION_LOG_KEY in state
