"""Deterministic timing evaluation for the Strands migration (component 02).

Ports the arithmetic from :mod:`agents.timing_evaluator` into a single
pure-Python ``@tool`` function. The orchestrator (component 14) calls
this directly after :mod:`strands_agents.audio_tool` has completed audio
tasks for a given scene set.

Two tolerance modes — **both must be preserved** per
``docs/strands-migration/components/02-timing-evaluator.md``:

* Typed ``BriefIntent`` path (preferred): caller passes
  ``intent_target_sec`` (> 0) and the tool compares the **movie runtime**
  (WhisperX total + inter-voice/inter-scene gaps) against that target
  with an absolute ±2 s tolerance.
* Legacy path: caller omits ``intent_target_sec`` (or passes ``None``/0)
  and the tool compares the **raw WhisperX narration total** against
  ``target_duration_sec`` with the ``max(target*0.15, 5.0)`` tolerance.

Per-scene deviation uses the legacy percent/minimum tolerance regardless
of mode — the intent-level ±2 s bound is a movie-level invariant, scenes
still get the classic ``±15 % / ±5 s`` floor.

This module contains **no LLM calls**. Any change to the tolerance
constants is a separate RFC.
"""

from __future__ import annotations

import logging
from typing import Any

from strands import tool

logger = logging.getLogger(__name__)


# Tolerance constants — ported verbatim from
# ``server/agents/timing_evaluator.py`` lines 42-48.
_TIMING_TOLERANCE_SEC: float = 2.0
"""Absolute tolerance (seconds) when a typed ``BriefIntent`` is available."""

_TIMING_TOLERANCE_PCT: float = 0.15
"""Legacy percentage tolerance, applied when no typed intent is present."""

_TIMING_TOLERANCE_MIN_SEC: float = 5.0
"""Legacy minimum tolerance floor, applied when no typed intent is present."""

# Gap overhead constants — ported verbatim from
# ``server/agents/timing_evaluator.py`` lines 146-147.
_INTER_VOICE_PAUSE: float = 1.5
"""Silent pause (seconds) inserted between consecutive voices in a scene."""

_INTER_SCENE_PAUSE: float = 2.5
"""Silent pause (seconds) inserted between consecutive scenes."""


def _gap_overhead_sec(scenes: list[dict[str, Any]]) -> float:
    """Compute total silence overhead that will sit on the OTIO timeline.

    Mirrors ``callbacks.deterministic_steps`` and ``callbacks.intent_gate``
    so every layer agrees on what "movie duration" means.

    Args:
        scenes: Scene objects carrying a ``voices`` list; each voice is
            considered *active* when its ``text`` field is non-empty.

    Returns:
        Total gap overhead in seconds.
    """
    total_voice_gaps = 0.0
    for scene in scenes:
        voices = scene.get("voices") or []
        active = sum(1 for v in voices if (v.get("text") or "").strip())
        total_voice_gaps += max(0, active - 1) * _INTER_VOICE_PAUSE
    total_scene_gaps = max(0, len(scenes) - 1) * _INTER_SCENE_PAUSE
    return total_voice_gaps + total_scene_gaps


def _per_scene_analysis(
    scenes: list[dict[str, Any]],
    per_scene_alignment: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute per-scene deviation using the legacy percent/min tolerance.

    The intent-level ±2 s bound is a *movie-level* invariant — individual
    scene deviations still use the more forgiving ``max(target*0.15, 5 s)``
    tolerance so scenes aren't force-failed when the overall runtime is on
    target.

    Args:
        scenes: Scene objects carrying per-scene targets.
        per_scene_alignment: Ordered list of WhisperX per-scene results,
            each carrying ``duration_sec`` (actual narration) and
            ``scene_id`` (or falls back to ``scene_num`` / index).

    Returns:
        Per-scene analysis entries with deviation, tolerance, and ``ok``
        flag. Entries beyond ``min(len(scenes), len(alignment))`` are
        dropped silently — callers receive the intersection only.
    """
    out: list[dict[str, Any]] = []
    for scene, seg in zip(scenes, per_scene_alignment, strict=False):
        target = float(
            scene.get("target_duration_sec") or scene.get("duration_sec") or 0.0
        )
        actual = float(seg.get("duration_sec", 0.0))
        scene_tolerance_sec = max(
            target * _TIMING_TOLERANCE_PCT, _TIMING_TOLERANCE_MIN_SEC
        )
        scene_dev_sec = actual - target
        scene_id = (
            seg.get("scene_id") or scene.get("scene_id") or scene.get("scene_num")
        )
        out.append(
            {
                "scene_id": scene_id,
                "target_sec": target,
                "actual_sec": actual,
                "deviation_sec": scene_dev_sec,
                "tolerance_sec": scene_tolerance_sec,
                "ok": abs(scene_dev_sec) <= scene_tolerance_sec,
            }
        )
    return out


def compute_timing_report(
    scenes: list[dict[str, Any]],
    whisperx_alignment: dict[str, Any],
    target_duration_sec: float,
    intent_target_sec: float | None = None,
) -> dict[str, Any]:
    """Compute the full ``{"timing_passed", "timing_report"}`` payload.

    This is the pure-Python core the ``@tool`` wrapper delegates to; it
    is exposed so the orchestrator (component 14) can call it directly
    from non-Strands code paths (e.g. when replaying a session) without
    going through the Strands tool-call dispatch machinery.

    Args:
        scenes: Scene objects; see :func:`_gap_overhead_sec` for voice
            activeness semantics.
        whisperx_alignment: Output from the audio tool; must carry
            ``total_duration_sec`` (number) and ``per_scene`` (list).
        target_duration_sec: Target duration used in the legacy tolerance
            path. Ignored when ``intent_target_sec`` is set.
        intent_target_sec: Typed ``BriefIntent.duration_sec`` if
            available. When set and positive, the function switches to
            the absolute ±2 s movie-runtime tolerance.

    Returns:
        ``{"timing_passed": bool, "timing_report": dict}``. See the
        module docstring for the two modes; the report always carries
        ``mode``, ``target_duration_sec``, ``actual_duration_sec``,
        ``gap_overhead_sec``, ``movie_duration_sec``, ``deviation_sec``,
        ``tolerance_sec``, ``per_scene_analysis``, and ``violations``.

    Raises:
        KeyError: If ``whisperx_alignment`` is missing
            ``total_duration_sec`` or ``per_scene``.
        ValueError: If ``target_duration_sec`` is non-positive while
            ``intent_target_sec`` is also non-positive (no valid target).
    """
    if "total_duration_sec" not in whisperx_alignment:
        raise KeyError("whisperx_alignment.total_duration_sec is required")
    if "per_scene" not in whisperx_alignment:
        raise KeyError("whisperx_alignment.per_scene is required")

    use_intent_mode = intent_target_sec is not None and float(intent_target_sec) > 0
    if not use_intent_mode and target_duration_sec <= 0:
        raise ValueError(
            "target_duration_sec must be positive when intent_target_sec is not provided"
        )

    actual_duration = float(whisperx_alignment["total_duration_sec"])
    gap_overhead = _gap_overhead_sec(scenes)
    movie_duration = actual_duration + gap_overhead

    if use_intent_mode:
        # intent path — compare movie runtime to typed target
        intent_target = float(intent_target_sec)  # type: ignore[arg-type]  # checked via use_intent_mode
        deviation_sec = movie_duration - intent_target
        tolerance_sec = _TIMING_TOLERANCE_SEC
        target_for_report: float = intent_target
        compared_duration = movie_duration
        mode = "intent"
    else:
        # legacy path — compare narration total to scene-sum target
        deviation_sec = actual_duration - target_duration_sec
        tolerance_sec = max(
            target_duration_sec * _TIMING_TOLERANCE_PCT,
            _TIMING_TOLERANCE_MIN_SEC,
        )
        target_for_report = target_duration_sec
        compared_duration = actual_duration
        mode = "legacy"

    per_scene = _per_scene_analysis(scenes, whisperx_alignment["per_scene"])

    violations: list[str] = []
    if abs(deviation_sec) > tolerance_sec:
        violations.append(
            "total_duration=<%.2f>, target=<%.2f>, deviation_sec=<%+.2f>, "
            "tolerance_sec=<%.2f>"
            % (compared_duration, target_for_report, deviation_sec, tolerance_sec)
        )
    violations.extend(
        f"scene {s['scene_id']} off by {s['deviation_sec']:+.2f}s "
        f"(tol {s['tolerance_sec']:.2f}s)"
        for s in per_scene
        if not s["ok"]
    )

    report: dict[str, Any] = {
        "mode": mode,
        "target_duration_sec": target_for_report,
        "actual_duration_sec": actual_duration,
        "gap_overhead_sec": gap_overhead,
        "movie_duration_sec": movie_duration,
        "deviation_sec": deviation_sec,
        "tolerance_sec": tolerance_sec,
        "per_scene_analysis": per_scene,
        "violations": violations,
    }
    timing_passed = not violations

    logger.info(
        "mode=<%s>, target=<%.2f>, compared=<%.2f>, deviation=<%+.2f>, "
        "tolerance=<%.2f>, passed=<%s> | timing evaluation",
        mode,
        target_for_report,
        compared_duration,
        deviation_sec,
        tolerance_sec,
        timing_passed,
    )
    return {"timing_passed": timing_passed, "timing_report": report}


@tool
def evaluate_timing(
    scenes: list[dict[str, Any]],
    whisperx_alignment: dict[str, Any],
    target_duration_sec: float,
    intent_target_sec: float | None = None,
) -> dict[str, Any]:
    """Compare narration duration to target; fail if outside tolerance.

    Dual-mode — see the module docstring. The orchestrator is expected
    to write the returned ``timing_passed`` / ``timing_report`` onto its
    own state after the tool call completes.

    Args:
        scenes: Scene objects carrying ``voices[].text`` and per-scene
            targets (``target_duration_sec`` or ``duration_sec``).
        whisperx_alignment: WhisperX output with ``total_duration_sec``
            and ``per_scene``.
        target_duration_sec: Legacy target (from brief / blackboard).
        intent_target_sec: Typed ``BriefIntent.duration_sec`` if
            available. When set and positive, switches to the ±2 s
            absolute tolerance path.

    Returns:
        ``{"timing_passed": bool, "timing_report": {...}}``.
    """
    return compute_timing_report(
        scenes=scenes,
        whisperx_alignment=whisperx_alignment,
        target_duration_sec=target_duration_sec,
        intent_target_sec=intent_target_sec,
    )
