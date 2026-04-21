"""Component 01 atoms — scenario structural checks + pure helpers.

Three atoms extracted from ``scenario_agent.py``:

* :func:`evaluate_scenario_structural` — deterministic structural check
  over a scene list. Wraps
  ``tools.scenario_evaluator_checks.run_all_structural_checks`` and
  packages the verdict + issues in the shape the orchestrator consumes.
* :func:`sum_scenario_duration` — total of ``target_duration_sec``
  across all scenes.
* :func:`derive_scenario_topic` — topic string derived from the first
  scene's title. Used by ``create_timeline`` to pick the OTIO filename.

The tool ``evaluate_scenario`` in ``scenario_agent.py`` simply wraps
:func:`evaluate_scenario_structural`; the rest of the module
(``generate_scenario``, ``refine_scenario``) is a connector that calls
injected LLM helpers and is not pure.
"""

from __future__ import annotations

from typing import Any

from strands_agents.scenario_agent import _derive_topic, _sum_duration
from tools.scenario_evaluator_checks import (
    EvaluatorReport,
    run_all_structural_checks,
)


def evaluate_scenario_structural(
    scenes: list[dict[str, Any]],
    style_lock: dict[str, Any],
    target_duration_sec: float,
) -> dict[str, Any]:
    """Run the deterministic structural checks on a scene list.

    Args:
        scenes: Candidate scene list.
        style_lock: Movie-level style lock the scenes were generated
            under. Must carry ``positive_fragment`` and
            ``forbidden_styles`` at minimum.
        target_duration_sec: User-requested total documentary length.

    Returns:
        ``{"rating": "EXCELLENT|GOOD|FAIR|POOR",
           "issues": [...], "suggestions": [...]}``.
    """
    scenario = {"scenes": scenes, "style_lock": style_lock}
    report: EvaluatorReport = run_all_structural_checks(
        scenario,
        target_duration_sec=target_duration_sec,
    )
    issues = [r.as_dict() for r in report.results if not r.passed]
    suggestions = [r.details for r in report.results if not r.passed and r.details]
    return {
        "rating": report.overall,
        "issues": issues,
        "suggestions": suggestions,
    }


def sum_scenario_duration(scenes: list[dict[str, Any]]) -> float:
    """Return the total narration duration across all scenes.

    Reads ``duration_sec`` first, then ``duration``, then 0.0. Skips
    non-dict entries and non-numeric values silently to match the
    upstream behaviour of :func:`scenario_agent._sum_duration`.
    """
    return _sum_duration(scenes)


def derive_scenario_topic(scenes: list[dict[str, Any]]) -> str:
    """Derive a filename-safe topic string from the first scene's title.

    Returns ``"documentary"`` when the first scene has no ``title`` or
    the title is blank. Caps length at 60 characters to keep OTIO
    paths sane.

    Raises:
        IndexError: If ``scenes`` is an empty list. Matches the
            upstream behaviour of
            :func:`scenario_agent._derive_topic`; callers reach this
            helper only after scenario generation has produced at
            least one scene, so empty-list is treated as a programmer
            error rather than a silent fallback.
    """
    return _derive_topic(scenes)


__all__ = [
    "derive_scenario_topic",
    "evaluate_scenario_structural",
    "sum_scenario_duration",
]
