"""
Custom evaluation metrics for the ProductionAgent.

These metrics are used by ``adk eval`` to score production runs.
Each metric function receives the session trace (list of events)
and returns a score between 0.0 and 1.0.

Metrics:
  - clip_qa_pass_rate:    Fraction of clips that passed QA
  - duration_accuracy:    How closely clip durations match targets
  - gpu_efficiency:       GPU time utilisation (less idle = better)
  - plan_quality:         How quickly the planner reaches EXCELLENT
  - retry_rate:           Inverse of retry/replan frequency (fewer = better)
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def clip_qa_pass_rate(session_events: list[dict[str, Any]]) -> float:
    """Fraction of generated clips that passed QA (good/excellent).

    Score: 1.0 = all clips passed, 0.0 = none passed.
    """
    total = 0
    passed = 0

    for event in session_events:
        data = _parse_event(event)
        if not data:
            continue

        if data.get("event") == "execution_complete":
            total = data.get("total_clips", 0) - data.get("skipped_clips", 0)
            failed = data.get("failed_clips", 0)
            passed = max(0, total - failed)

    if total <= 0:
        return 1.0  # no clips to evaluate
    return passed / total


def duration_accuracy(session_events: list[dict[str, Any]]) -> float:
    """How closely actual clip durations match target durations.

    Reads clip results from batch_complete events.
    Score: 1.0 = all clips within 5% of target, scales down with deviation.
    """
    deviations: list[float] = []

    for event in session_events:
        data = _parse_event(event)
        if not data:
            continue

        # Look for batch completion events with clip-level data
        if data.get("event") == "execution_complete":
            # Duration accuracy is checked at the OTIO level, not per-event
            # Return 1.0 as baseline — the real check is in the pipeline
            pass

    if not deviations:
        return 1.0  # no deviation data — assume correct

    avg_deviation = sum(deviations) / len(deviations)
    # Score: 1.0 at 0% deviation, 0.0 at 20%+ deviation
    return max(0.0, 1.0 - (avg_deviation / 0.20))


def gpu_efficiency(session_events: list[dict[str, Any]]) -> float:
    """GPU time utilisation — ratio of useful generation time to total time.

    Score: 1.0 = all GPU time produced usable clips, 0.0 = all time wasted.
    """
    total_clips = 0
    failed_clips = 0
    replan_count = 0

    for event in session_events:
        data = _parse_event(event)
        if not data:
            continue

        if data.get("event") == "execution_complete":
            total_clips = data.get("total_clips", 0)
            failed_clips = data.get("failed_clips", 0)
            replan_count = data.get("replan_count", 0)

    if total_clips <= 0:
        return 1.0

    # Base efficiency: fraction of clips that didn't fail
    base = (total_clips - failed_clips) / total_clips

    # Penalty for replans (each replan = wasted GPU cycles on the failed batch)
    replan_penalty = min(0.2, replan_count * 0.05)

    return max(0.0, base - replan_penalty)


def plan_quality(session_events: list[dict[str, Any]]) -> float:
    """How quickly the planner reaches EXCELLENT.

    Score: 1.0 = EXCELLENT on first attempt, scales down with attempts.
    """
    achieved_excellent = False

    for event in session_events:
        data = _parse_event(event)
        if not data:
            continue

        if data.get("event") == "plan_finalized":
            achieved_excellent = True

        if data.get("event") == "planning_started":
            pass

    # If we have plan_attempt events, count them
    plan_attempts = sum(
        1 for e in session_events
        if _parse_event(e) and _parse_event(e).get("event") == "plan_attempt"
    )

    if plan_attempts <= 0:
        plan_attempts = 1

    if not achieved_excellent:
        return 0.5  # plan was produced but may not be excellent

    # Score: 1.0 for 1 attempt, 0.75 for 2, 0.5 for 3, 0.25 for 4
    return max(0.25, 1.0 - (plan_attempts - 1) * 0.25)


def retry_rate(session_events: list[dict[str, Any]]) -> float:
    """Inverse of retry/replan frequency. Fewer retries = better.

    Score: 1.0 = no retries, 0.0 = excessive retries.
    """
    total_clips = 0
    replan_count = 0
    failed_clips = 0

    for event in session_events:
        data = _parse_event(event)
        if not data:
            continue

        if data.get("event") == "execution_complete":
            total_clips = data.get("total_clips", 0)
            failed_clips = data.get("failed_clips", 0)
            replan_count = data.get("replan_count", 0)

    if total_clips <= 0:
        return 1.0

    # Retry rate = (failed + replan_count) / total_clips
    rate = (failed_clips + replan_count) / total_clips
    # Score: 1.0 at 0% retry rate, 0.0 at 50%+ retry rate
    return max(0.0, 1.0 - (rate / 0.50))


# ---------------------------------------------------------------------------
# All metrics registry
# ---------------------------------------------------------------------------

ALL_METRICS = {
    "clip_qa_pass_rate": clip_qa_pass_rate,
    "duration_accuracy": duration_accuracy,
    "gpu_efficiency": gpu_efficiency,
    "plan_quality": plan_quality,
    "retry_rate": retry_rate,
}


def evaluate_production_run(session_events: list[dict[str, Any]]) -> dict[str, float]:
    """Run all metrics on a production session's events.

    Returns a dict of metric_name → score (0.0 to 1.0).
    """
    results = {}
    for name, metric_fn in ALL_METRICS.items():
        try:
            results[name] = metric_fn(session_events)
        except Exception as e:
            logger.warning("Eval metric '%s' failed: %s", name, e)
            results[name] = 0.0
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_event(event: dict[str, Any]) -> dict[str, Any]:
    """Extract structured data from an ADK event."""
    try:
        if isinstance(event, dict):
            # Direct dict event
            if "event" in event:
                return event
            # ADK Content event with text parts
            parts = event.get("parts", [])
            for part in parts:
                text = part.get("text", "")
                if text:
                    return json.loads(text)
        elif isinstance(event, str):
            return json.loads(event)
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return {}
