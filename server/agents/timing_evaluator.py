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

# Tolerance: allow ±15% deviation from target duration before triggering
# a refinement loop.  Tighter than the original ±20% in Strands because
# the ADK pipeline has fewer retry opportunities.
_TIMING_TOLERANCE_PCT = 0.15

# Minimum absolute deviation (seconds) to trigger refinement.
# Prevents micro-adjustments on short movies where 15% is < 5s.
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
    if actual_duration <= 0:
        raw_alignment = state.get("whisperx_alignment", "{}")
        try:
            alignment = json.loads(str(raw_alignment)) if isinstance(raw_alignment, str) else raw_alignment
            if isinstance(alignment, dict):
                for scene_data in alignment.values():
                    if isinstance(scene_data, dict):
                        actual_duration = max(
                            actual_duration,
                            scene_data.get("end_time", 0),
                        )
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

    # Compute deviation
    deviation_sec = actual_duration - target_duration
    deviation_pct = abs(deviation_sec) / target_duration if target_duration > 0 else 0
    tolerance_sec = max(
        target_duration * _TIMING_TOLERANCE_PCT,
        _TIMING_TOLERANCE_MIN_SEC,
    )

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
        "deviation_sec": round(deviation_sec, 1),
        "deviation_pct": round(deviation_pct * 100, 1),
        "tolerance_sec": round(tolerance_sec, 1),
        "passed": passed,
        "scene_analysis": scene_analysis,
        "over_budget": deviation_sec > 0,
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
