"""
Timing Evaluator — checks audio duration against target budget.

This agent is part of the timing feedback loop (R3 from the deep audit).
It runs after audio generation and evaluates whether the total narration
duration is within acceptable tolerance of the target.  If not, it sets
state["timing_passed"] = False so the scenario refiner can adjust.

Architecture (inside the timing_loop LoopAgent)::

    LoopAgent("timing_loop", max_iterations=3)
    ├── Agent("audio_agent")          # TTS + WhisperX
    ├── Agent("timing_evaluator")     # THIS — checks duration budget
    └── Agent("scenario_refiner")     # adjusts scenes if timing fails

The evaluator is deterministic (before_agent_callback does all the work)
to avoid unreliable LLM tool-calling for a simple arithmetic check.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.genai import types as genai_types

from agents.model_config import build_model

logger = logging.getLogger(__name__)

# Tolerance: how far the measured movie runtime may drift from the
# user's stated target before the loop re-runs.  User request ("aim
# for movie that is exactly 7 minutes, milliseconds not important"):
# keep this tight — ±2 whole seconds of total film runtime.  The
# scenario refiner regenerates narration until the loop passes; the
# timing_loop LoopAgent is now capped at 10 iterations (see
# TIMING_LOOP_MAX_ITERATIONS) after which a human escalation fires.
_TIMING_TOLERANCE_SEC: float = 2.0

# Legacy percent/minimum knobs, kept for back-compat with tests and
# briefs that don't carry a typed intent; superseded by the absolute
# tolerance above when the BriefIntent is available on the blackboard.
_TIMING_TOLERANCE_PCT = 0.15
_TIMING_TOLERANCE_MIN_SEC = 5.0


def _evaluate_timing(callback_context: CallbackContext) -> Optional[genai_types.Content]:
    """Deterministic timing evaluation — runs before the LLM agent.

    Compares total audio duration (from OTIO timeline or alignment data)
    against the target duration budget derived from scene durations.

    Sets state["timing_passed"] = True/False and state["timing_analysis"]
    with detailed breakdown for the scenario refiner.
    """
    state = callback_context.state

    # Parse scenes to compute target duration
    from callbacks.deterministic_steps import extract_json_array
    raw_scenes = state.get("scenes", "[]")
    scenes = extract_json_array(str(raw_scenes))
    if not scenes:
        logger.warning("Timing evaluator: no scenes found, passing by default")
        state["timing_passed"] = True
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="TIMING: No scenes found — skipping evaluation.")],
        )

    # Target: user's stated film runtime from the typed BriefIntent
    # when present (the canonical source of truth).  Fall back to the
    # scene-sum on legacy runs without an intent.  Comparing against the
    # user's ground truth (e.g. 420s for "7 minutes") is what lets the
    # timing loop regenerate audio until the delivered mp4 matches what
    # the user actually asked for.
    target_duration: float
    intent_target: Optional[float] = None
    try:
        from agents.intent_extractor import get_brief_intent

        intent = get_brief_intent(state)
        if intent is not None:
            intent_target = float(intent.duration_sec)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("Timing evaluator: intent lookup failed: %s", exc)

    if intent_target is not None and intent_target > 0:
        target_duration = intent_target
    else:
        target_duration = sum(s.get("duration_sec", 30) for s in scenes)

    # Get actual audio duration from OTIO timeline
    actual_duration = 0.0
    timeline_path = state.get("_timeline_path", "")
    if timeline_path and os.path.exists(timeline_path):
        try:
            import opentimelineio as otio
            tl = otio.adapters.read_from_file(timeline_path)
            for track in tl.tracks:
                if track.name and "narration" in track.name.lower():
                    for item in track:
                        if isinstance(item, otio.schema.Clip) and item.source_range:
                            actual_duration += item.source_range.duration.to_seconds()
                    break
        except Exception as e:
            logger.warning("Timing evaluator: OTIO read failed: %s", e)

    # Fallback: estimate from WhisperX alignment data
    # Each alignment entry's end_time is relative to the start of that
    # individual voice clip, so we SUM them to get total narration duration.
    # Using max() would give the longest single clip (~10-30s), not the
    # total movie narration (~300s for a 5-min doc), causing a massive
    # underestimate that always triggers unnecessary refinement.
    if actual_duration <= 0:
        raw_alignment = state.get("whisperx_alignment", "{}")
        try:
            alignment = json.loads(str(raw_alignment)) if isinstance(raw_alignment, str) else raw_alignment
            if isinstance(alignment, dict):
                for scene_data in alignment.values():
                    if isinstance(scene_data, dict):
                        actual_duration += scene_data.get("end_time", 0)
        except (json.JSONDecodeError, TypeError):
            pass

    if actual_duration <= 0:
        logger.warning("Timing evaluator: could not determine actual duration, passing")
        state["timing_passed"] = True
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(
                text="TIMING: Could not determine actual duration — skipping evaluation."
            )],
        )

    # Compute the full movie runtime the audio stage will deliver:
    # measured narration + inter-voice and inter-scene silence gaps.
    # These gap constants match ``callbacks.deterministic_steps`` /
    # ``callbacks.intent_gate`` so every layer agrees on what duration
    # means.  The movie runtime is what the user experiences and what
    # ffprobe reports on the final mp4 — that's what we compare
    # against the target.
    _INTER_VOICE_PAUSE = 1.5
    _INTER_SCENE_PAUSE = 2.5
    total_voice_gaps = 0.0
    for s in scenes:
        voices = s.get("voices") or []
        active = sum(1 for v in voices if (v.get("text") or "").strip())
        total_voice_gaps += max(0, active - 1) * _INTER_VOICE_PAUSE
    total_scene_gaps = max(0, len(scenes) - 1) * _INTER_SCENE_PAUSE
    gap_overhead_sec = total_voice_gaps + total_scene_gaps
    movie_duration = actual_duration + gap_overhead_sec

    # Deviation is computed on MOVIE runtime when we have a typed intent
    # (user's "7 minutes" → compare against 420s of delivered mp4).
    # Otherwise fall back to narration-vs-scene-budget for legacy runs.
    if intent_target is not None and intent_target > 0:
        deviation_sec = movie_duration - target_duration
        tolerance_sec = _TIMING_TOLERANCE_SEC
    else:
        deviation_sec = actual_duration - target_duration
        tolerance_sec = max(
            target_duration * _TIMING_TOLERANCE_PCT,
            _TIMING_TOLERANCE_MIN_SEC,
        )
    deviation_pct = abs(deviation_sec) / target_duration if target_duration > 0 else 0

    passed = abs(deviation_sec) <= tolerance_sec

    # Per-scene breakdown for the refiner
    scene_analysis = []
    for s in scenes:
        sn = s.get("scene_num", 0)
        target = s.get("duration_sec", 30)
        # Estimate actual per-scene from word count (150 wpm)
        total_words = 0
        for voice in s.get("voices", []):
            text = voice.get("text", "")
            total_words += len(text.split())
        estimated_actual = total_words / 2.5  # ~150 wpm = 2.5 words/sec
        scene_analysis.append({
            "scene_num": sn,
            "target_sec": target,
            "estimated_actual_sec": round(estimated_actual, 1),
            "deviation_sec": round(estimated_actual - target, 1),
        })

    # Store analysis for the refiner
    analysis = {
        "target_duration": round(target_duration, 1),
        "actual_duration": round(actual_duration, 1),
        "movie_duration": round(movie_duration, 1),
        "gap_overhead_sec": round(gap_overhead_sec, 1),
        "deviation_sec": round(deviation_sec, 1),
        "deviation_pct": round(deviation_pct * 100, 1),
        "tolerance_sec": round(tolerance_sec, 1),
        "passed": passed,
        "scene_analysis": scene_analysis,
        "over_budget": deviation_sec > 0,
        "intent_target_sec": (
            round(intent_target, 1) if intent_target else None
        ),
    }
    state["timing_passed"] = passed
    state["timing_analysis"] = json.dumps(analysis)

    direction = "OVER" if deviation_sec > 0 else "UNDER"
    verdict = "PASS" if passed else "FAIL"

    logger.info(
        "target=<%.1f>, actual=<%.1f>, deviation=<%.1f> (%.1f%%), tolerance=<%.1f> | "
        "timing evaluation %s",
        target_duration, actual_duration, deviation_sec,
        deviation_pct * 100, tolerance_sec, verdict,
    )

    summary = (
        f"TIMING {verdict}: actual={actual_duration:.1f}s vs target={target_duration:.1f}s "
        f"({direction} by {abs(deviation_sec):.1f}s / {deviation_pct*100:.1f}%). "
        f"Tolerance: ±{tolerance_sec:.1f}s."
    )

    if not passed:
        # Add specific guidance for the refiner
        over_scenes = [s for s in scene_analysis if s["deviation_sec"] > 3]
        under_scenes = [s for s in scene_analysis if s["deviation_sec"] < -3]
        if over_scenes:
            summary += f"\nScenes over budget: {[s['scene_num'] for s in over_scenes]}"
        if under_scenes:
            summary += f"\nScenes under budget: {[s['scene_num'] for s in under_scenes]}"

    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(text=summary)],
    )


timing_evaluator = Agent(
    name="timing_evaluator",
    model=build_model(synthesis=True),
    instruction="You evaluate audio timing compliance. This is handled automatically.",
    tools=[],
    before_agent_callback=_evaluate_timing,
)
