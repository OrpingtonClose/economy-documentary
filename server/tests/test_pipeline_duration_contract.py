"""End-to-end deterministic contract test.

This test exercises the *whole* deterministic chain the pipeline runs
on any 7-minute documentary brief, in the exact order the runtime
drives it:

    brief → extract_intent → evaluate_gate → clean_scenes_after_scenario
          → verify("scenario") → verify("audio") → _evaluate_timing

None of these steps require GPU workers or LLMs — they're all pure
Python.  The three bugs that halted PAG run #1, #2, #3 and the
fourth one that Devin Review caught on #282 all lived here and were
missable by unit tests because no single unit test exercised the
chain.  This file is the safety net that makes sure whenever someone
touches any one of these files, the chain still agrees with itself.

Regressions this test would have caught if it had existed earlier:
    * #272 ``state.pop()`` — would have crashed at ``extract_intent``.
    * #272 audience leak — asserts ``"adhd"`` never in required_topics.
    * #282 gate/verifier metric mismatch — exercises both on the same
      scenes, post-scaling, and demands both PASS when they should.
    * #283 audio verifier on wrong metric — exercises ``_verify_audio``
      after ``_evaluate_timing`` passes, which is the *exact* order
      the pipeline runs them.  Before the fix this always HALTed.

The point of this file is not to unit-test any one function — it's
to assert the CONTRACT between them.  Every unit test on its own
passed while the contract was broken in production.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from agents.intent_extractor import (  # noqa: E402
    BRIEF_INTENT_KEY,
    extract_intent,
)
from agents.timing_evaluator import _evaluate_timing  # noqa: E402
from callbacks.intent_gate import evaluate_gate  # noqa: E402
from callbacks.intent_verifier import (  # noqa: E402
    STAGE_AUDIO,
    STAGE_SCENARIO,
    verify_stage_constraints,
)


# ---------------------------------------------------------------------------
# Fixtures — a realistic 7-minute PAG brief
# ---------------------------------------------------------------------------


PAG_BRIEF = (
    "Make a 7-minute ADHD-friendly documentary about the Periaqueductal "
    "Gray (PAG).  It must cover opioid chemistry and fight-flight-freeze "
    "circuitry.  Do not discuss recreational drug use."
)


def _scene(scene_num: int, duration_sec: float, *, voices: int = 3) -> dict:
    """Build a realistic 3-voice scene matching the scenario director output."""
    return {
        "scene_num": scene_num,
        "title": f"Scene {scene_num}",
        "duration_sec": duration_sec,
        "voices": [
            {"voice": f"V{i + 1}", "text": f"PAG narration line {i + 1}"}
            for i in range(voices)
        ],
        "narration": (
            "PAG opioid chemistry drives fight-flight-freeze circuitry "
            "in the midbrain."
        ),
    }


def _simulate_scaling(scenes: list[dict], target_sec: float) -> list[dict]:
    """Mimic ``deterministic_steps.clean_scenes_after_scenario`` scaling.

    The runtime scales scene durations DOWN so that
    ``sum(duration_sec) + total_gap_overhead = target_sec``.  We
    replicate that math here so the test drives the pipeline in the
    same state it reaches in production.
    """
    _INTER_VOICE_PAUSE = 1.5
    _INTER_SCENE_PAUSE = 2.5

    total_voice_gaps = 0.0
    for s in scenes:
        voices = s.get("voices") or []
        active = sum(1 for v in voices if (v.get("text") or "").strip())
        total_voice_gaps += max(0, active - 1) * _INTER_VOICE_PAUSE
    total_scene_gaps = max(0, len(scenes) - 1) * _INTER_SCENE_PAUSE
    gap_overhead = total_voice_gaps + total_scene_gaps

    narration_budget = target_sec - gap_overhead
    original_total = sum(s.get("duration_sec", 0) for s in scenes)
    if original_total <= 0:
        return scenes
    scale = narration_budget / original_total

    scaled = []
    for s in scenes:
        s2 = dict(s)
        s2["duration_sec"] = round(s.get("duration_sec", 0) * scale, 2)
        scaled.append(s2)
    return scaled


# ---------------------------------------------------------------------------
# The contract test
# ---------------------------------------------------------------------------


class TestSevenMinutePAGContract:
    """Every stage in the 7-min PAG deterministic chain must agree.

    This is the test that would have prevented run #1, #2, #3, and
    the #282/#283 halt.  If *any* link in the chain rejects a valid
    7-min scenario, this test fails.
    """

    def test_intent_extractor_reads_seven_minutes(self):
        intent = extract_intent(PAG_BRIEF, use_llm=False)
        assert intent.duration_sec == pytest.approx(420.0, abs=0.5)

    def test_intent_extractor_does_not_leak_audience_into_topics(self):
        """Run #3 halted because 'ADHD' was emitted as a required topic
        and no PAG-anatomy scene mentioned the literal token."""
        intent = extract_intent(PAG_BRIEF, use_llm=False)
        topics_lower = {t.lower() for t in intent.required_topics}
        assert "adhd" not in topics_lower
        assert "adhd-friendly" not in topics_lower
        assert intent.audience == "adhd-friendly"

    def test_gate_passes_on_scenario_sized_for_movie_target(self):
        """A competent director produces scenes whose narration + gaps
        land inside the 420s window.  The gate must accept them."""
        intent = extract_intent(PAG_BRIEF, use_llm=False)
        raw_scenes = [_scene(i, 420.0 / 12) for i in range(1, 13)]
        scenes = _simulate_scaling(raw_scenes, intent.duration_sec)
        verdict = evaluate_gate(intent, scenes)
        assert verdict.passed is True, verdict.failures
        assert verdict.movie_duration_sec == pytest.approx(420.0, abs=1.0)

    def test_gate_rejects_90_second_scenario(self):
        """Run #2 quietly aimed for 90s.  The gate must reject that."""
        intent = extract_intent(PAG_BRIEF, use_llm=False)
        tiny_scenes = [_scene(i, 90.0 / 3) for i in range(1, 4)]
        verdict = evaluate_gate(intent, tiny_scenes)
        assert verdict.passed is False
        assert any("runtime" in f for f in verdict.failures)

    def test_scenario_verifier_agrees_with_gate(self):
        """Gate and verifier MUST use the same metric.  Run #3 failed
        because the gate used raw narration, the verifier used movie
        duration — a scenario passing the gate got halted by the
        verifier."""
        intent = extract_intent(PAG_BRIEF, use_llm=False)
        raw_scenes = [_scene(i, 420.0 / 12) for i in range(1, 13)]
        scenes = _simulate_scaling(raw_scenes, intent.duration_sec)
        state = {BRIEF_INTENT_KEY: intent.to_json(), "scenes": scenes}
        record = verify_stage_constraints(STAGE_SCENARIO, state)
        assert record.passed is True, record.failures
        assert record.metrics["movie_duration_sec"] == pytest.approx(
            420.0, abs=1.0
        )

    def test_audio_verifier_agrees_with_gate_on_scaled_scenes(self):
        """#283: the audio verifier was comparing narration-only against
        the target, which after scaling is ALWAYS short by ~gap_overhead.
        That caused a guaranteed HALT right after the timing loop passed.
        """
        intent = extract_intent(PAG_BRIEF, use_llm=False)
        raw_scenes = [_scene(i, 420.0 / 12) for i in range(1, 13)]
        scenes = _simulate_scaling(raw_scenes, intent.duration_sec)

        # Narration budget after scaling = target - gap_overhead.
        # WhisperX would measure this many seconds on the delivered wav.
        narration_total = sum(s["duration_sec"] for s in scenes)

        state = {
            BRIEF_INTENT_KEY: intent.to_json(),
            "scenes": scenes,
            "whisperx_alignment": json.dumps(
                {"total_duration_sec": narration_total}
            ),
        }
        record = verify_stage_constraints(STAGE_AUDIO, state)
        assert record.passed is True, record.failures
        assert record.metrics["movie_duration_sec"] == pytest.approx(
            420.0, abs=1.0
        )

    def test_timing_evaluator_passes_on_scaled_scenes(self):
        """Timing evaluator runs inside the timing_loop; it must
        agree with the verifier or the loop will spin forever or
        exhaust.  This validates the ±2s tight tolerance path."""
        intent = extract_intent(PAG_BRIEF, use_llm=False)
        raw_scenes = [_scene(i, 420.0 / 12) for i in range(1, 13)]
        scenes = _simulate_scaling(raw_scenes, intent.duration_sec)
        narration_total = sum(s["duration_sec"] for s in scenes)

        # Build a whisperx_alignment in the shape the evaluator reads:
        # top-level dict keyed by scene_id, each carrying end_time.
        alignment = {
            f"scene_{s['scene_num']}": {
                "end_time": narration_total / len(scenes),
            }
            for s in scenes
        }

        ctx = MagicMock()
        ctx.state = {
            BRIEF_INTENT_KEY: intent.to_json(),
            "scenes": json.dumps(scenes),
            "whisperx_alignment": json.dumps(alignment),
            "_timeline_path": "",
        }

        _evaluate_timing(ctx)

        assert ctx.state["timing_passed"] is True, (
            "timing_evaluator halted a scenario that the gate and the "
            "verifier both accept — they've drifted out of lockstep. "
            f"analysis={ctx.state.get('timing_analysis')}"
        )
        analysis = json.loads(ctx.state["timing_analysis"])
        assert analysis["movie_duration"] == pytest.approx(420.0, abs=2.0)
        assert analysis["passed"] is True

    def test_full_chain_no_false_halt(self):
        """The headline test: walk the exact chain the runtime walks
        on a 7-minute PAG brief and demand every step PASS.
        If this test fails, run a real pipeline and expect pain."""
        # Step 1: typed intent
        intent = extract_intent(PAG_BRIEF, use_llm=False)
        assert intent.duration_sec == pytest.approx(420.0, abs=0.5)
        assert "adhd" not in {t.lower() for t in intent.required_topics}

        # Step 2: scenario director produces scenes summing to target
        raw_scenes = [_scene(i, 420.0 / 12) for i in range(1, 13)]

        # Step 3: pre-flight gate on raw scenes — movie_duration is
        # 420 + gaps, outside the window.  Gate rejects as intended.
        pre_scale_verdict = evaluate_gate(intent, raw_scenes)
        assert pre_scale_verdict.passed is False

        # Step 4: deterministic_steps scales scenes so narration+gaps=target
        scenes = _simulate_scaling(raw_scenes, intent.duration_sec)

        # Step 5: gate accepts scaled scenes
        post_scale_verdict = evaluate_gate(intent, scenes)
        assert post_scale_verdict.passed is True, post_scale_verdict.failures

        # Step 6: scenario stage verifier accepts same scenes
        state = {
            BRIEF_INTENT_KEY: intent.to_json(),
            "scenes": scenes,
        }
        scenario_record = verify_stage_constraints(STAGE_SCENARIO, state)
        assert scenario_record.passed is True, scenario_record.failures

        # Step 7: audio verifier accepts after narration is measured
        narration_total = sum(s["duration_sec"] for s in scenes)
        state["whisperx_alignment"] = json.dumps(
            {"total_duration_sec": narration_total}
        )
        audio_record = verify_stage_constraints(STAGE_AUDIO, state)
        assert audio_record.passed is True, audio_record.failures

        # Step 8: timing loop passes on same inputs
        alignment = {
            f"scene_{s['scene_num']}": {
                "end_time": narration_total / len(scenes),
            }
            for s in scenes
        }
        ctx = MagicMock()
        ctx.state = {
            BRIEF_INTENT_KEY: intent.to_json(),
            "scenes": json.dumps(scenes),
            "whisperx_alignment": json.dumps(alignment),
            "_timeline_path": "",
        }
        _evaluate_timing(ctx)
        assert ctx.state["timing_passed"] is True, (
            ctx.state.get("timing_analysis")
        )

        # Final consistency check: every measurement agrees.
        analysis = json.loads(ctx.state["timing_analysis"])
        assert abs(analysis["movie_duration"] - 420.0) < 2.0
        assert abs(
            post_scale_verdict.movie_duration_sec - 420.0
        ) < 2.0
        assert abs(
            scenario_record.metrics["movie_duration_sec"] - 420.0
        ) < 2.0
        assert abs(
            audio_record.metrics["movie_duration_sec"] - 420.0
        ) < 2.0
