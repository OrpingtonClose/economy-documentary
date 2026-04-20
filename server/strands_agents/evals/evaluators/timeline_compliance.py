"""TimelineComplianceEvaluator — deterministic OTIO structural checks.

Wraps the existing ``validate_otio_compliance`` logic from
``server/tools/validation_tools.py`` into the Evaluator protocol
without pulling in the ADK ``tool_context`` indirection. The result is
decomposed into one :class:`EvaluationOutput` per violation category,
plus a top-level ``timeline_loaded`` result, so downstream dashboards
can pivot on category rather than a single overall verdict.

Input shape
-----------
``EvaluationData`` with:

* ``input``: absolute path to the OTIO timeline, OR
* ``actual_output[`timeline_path`]`` / ``metadata[`timeline_path`]``
  as fallbacks.

Output
------
One :class:`EvaluationOutput` per structural check
(``timeline_loaded``, ``no_negative_duration``, ``media_references``,
``track_consistency``). Hard gate: any violation fails the case per
``CUSTOM_EVALUATORS.md`` §4.
"""

from __future__ import annotations

import os
from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput


class TimelineComplianceEvaluator(Evaluator[str, dict[str, Any]]):
    """Deterministic wrapper around OTIO compliance checks."""

    def __init__(self) -> None:
        super().__init__()

    def evaluate(
        self,
        evaluation_case: EvaluationData[str, dict[str, Any]],
    ) -> list[EvaluationOutput]:
        timeline_path = _resolve_timeline_path(evaluation_case)

        if not timeline_path:
            return [_fail("timeline_loaded", "no timeline_path supplied")]

        if not os.path.exists(timeline_path):
            return [_fail("timeline_loaded", f"timeline file not found: {timeline_path}")]

        try:
            import opentimelineio as otio
        except ImportError as exc:  # pragma: no cover — dep pinned in pyproject
            return [_fail("timeline_loaded", f"opentimelineio unavailable: {exc}")]

        try:
            tl = otio.adapters.read_from_file(timeline_path)
        except Exception as exc:
            return [_fail("timeline_loaded", f"failed to parse timeline: {exc}")]

        return _categorise_violations(tl, otio)


def _resolve_timeline_path(
    evaluation_case: EvaluationData[str, dict[str, Any]],
) -> str:
    if isinstance(evaluation_case.input, str) and evaluation_case.input:
        return evaluation_case.input
    actual = evaluation_case.actual_output or {}
    if isinstance(actual, dict) and actual.get("timeline_path"):
        return str(actual["timeline_path"])
    metadata = evaluation_case.metadata or {}
    if metadata.get("timeline_path"):
        return str(metadata["timeline_path"])
    return ""


def _categorise_violations(tl: Any, otio: Any) -> list[EvaluationOutput]:
    negative_duration: list[str] = []
    missing_media: list[str] = []

    for track in tl.tracks:
        track_name = track.name or "unnamed"
        for idx, item in enumerate(track):
            if not isinstance(item, (otio.schema.Clip, otio.schema.Gap)):
                continue
            if item.source_range and item.source_range.duration.to_seconds() < 0:
                negative_duration.append(f"{track_name}[{idx}]")
            if isinstance(item, otio.schema.Clip):
                ref = getattr(item, "media_reference", None)
                target_url = getattr(ref, "target_url", None) if ref else None
                if target_url and not os.path.exists(target_url):
                    missing_media.append(f"{track_name}[{idx}]={target_url}")

    narration_tracks = [t for t in tl.tracks if "narration" in (t.name or "").lower()]
    video_tracks = [t for t in tl.tracks if "video" in (t.name or "").lower()]
    track_consistency_ok = True
    track_consistency_reason = "narration/video clip counts non-zero or both empty"
    if narration_tracks and video_tracks:
        narr = sum(1 for i in narration_tracks[0] if isinstance(i, otio.schema.Clip))
        vid = sum(1 for i in video_tracks[0] if isinstance(i, otio.schema.Clip))
        if narr > 0 and vid == 0:
            track_consistency_ok = False
            track_consistency_reason = f"narration has {narr} clips but video has 0"

    return [
        _pass_or_fail(
            "timeline_loaded",
            True,
            f"loaded {len(tl.tracks)} tracks",
        ),
        _pass_or_fail(
            "no_negative_duration",
            not negative_duration,
            "no negative durations"
            if not negative_duration
            else f"negative durations at: {', '.join(negative_duration)}",
        ),
        _pass_or_fail(
            "media_references",
            not missing_media,
            "all media references exist"
            if not missing_media
            else f"missing media: {', '.join(missing_media)}",
        ),
        _pass_or_fail(
            "track_consistency",
            track_consistency_ok,
            track_consistency_reason,
        ),
    ]


def _pass_or_fail(label: str, passed: bool, reason: str) -> EvaluationOutput:
    return EvaluationOutput(
        score=1.0 if passed else 0.0,
        test_pass=passed,
        reason=f"{'PASS' if passed else 'FAIL'} {label}: {reason}",
        label=label,
    )


def _fail(label: str, reason: str) -> EvaluationOutput:
    return EvaluationOutput(
        score=0.0,
        test_pass=False,
        reason=f"FAIL {label}: {reason}",
        label=label,
    )
