"""Failure-mode video Experiment — deterministic QA gate corpus.

Runs :class:`FailureModeEvaluator` over the committed video fixtures
that carry an explicit ``expected_failure_mode`` classification, plus
a set of clean positive controls. All grading is offline and
deterministic — ffmpeg decodes the pixels, numpy runs the detectors,
no LLM calls anywhere.

This is the counterpart to :mod:`media_corpus` (which drives the live
multimodal judges). The failure-mode half of the corpus is graded
here, by pixel-level signals, because clear-cut reject classes
(frozen, black-out, white-out) do not belong in a judge's lap —
pixels tell the truth faster and more cheaply.

Invocation
----------
Idiomatic ``python -m`` entrypoint::

    python -m strands_agents.evals.experiments.failure_mode_video

Exit codes follow the repo-wide runner contract: ``0`` on full green,
``1`` on any failure, ``2`` on a structural skip (no fixtures on
disk, missing ffmpeg). CI invokes the module as a matrix cell in
``.github/workflows/strands-evals.yml``.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from strands_evals.case import Case
from strands_evals.experiment import Experiment

from strands_agents.evals._runner import run_experiment_as_main
from strands_agents.evals.evaluators import FailureModeEvaluator
from strands_agents.evals.evaluators.failure_mode_detectors import (
    VideoSignals,
    extract_signals,
)
from strands_agents.evals.fixtures.manifest import (
    FixtureEntry,
    load_manifest,
    resolve_fixture_path,
)

# Which fixtures are graded by the deterministic QA gate and which
# failure class each exercises. Kept explicit instead of inferred
# from ``axis`` strings so a new fixture with a weird axis name can
# be added without fear of silently mis-classifying the detector.
_FAILURE_MODE_FIXTURES: dict[str, str] = {
    "video_frozen": "frozen",
    "video_black": "black",
    "video_white": "white",
}

# Positive controls — clean clips that must NOT trip any detector.
# ``video_solid_black_color`` and ``video_solid_white_not_black`` are
# *excluded* from this list: they are semantically black/white clips
# used by the judge-facing corpus ("is this predominantly black?
# yes") and would legitimately trip the black/white detectors, which
# would be a false negative against the pipeline's QA intent.
_CLEAN_CONTROL_FIXTURES: tuple[str, ...] = (
    "video_hello_red",
    "video_goodbye_green",
    "video_solid_red",
    "video_solid_green",
    "video_solid_blue",
    "video_solid_yellow",
    "video_world_blue",
    "video_moving_text",
)


def _case_for_fixture(
    entry: FixtureEntry,
    *,
    expected_failure_mode: str,
) -> Case[dict[str, Any], dict[str, Any]]:
    """Build a :class:`Case` pointing at one fixture with a mode label."""
    fixture_input = {
        "id": entry.id,
        "axis": entry.axis,
        "media": entry.media,
        "relative_path": entry.relative_path,
        "sha256": entry.sha256,
        "expected_verdict": entry.expected_verdict,
        "prompt": entry.prompt,
    }
    return Case[dict[str, Any], dict[str, Any]](
        name=f"failure_mode.{expected_failure_mode}.{entry.id}",
        input=fixture_input,
        expected_output={"failure_mode": expected_failure_mode},
        metadata={
            "fixture_id": entry.id,
            "axis": entry.axis,
            "media": entry.media,
            "sha256": entry.sha256,
            "expected_failure_mode": expected_failure_mode,
        },
    )


def build_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Build the list of cases from the committed manifest.

    Returns:
        One :class:`Case` per reject fixture plus one per clean
        positive control. Silently skips any fixture id not present
        in the manifest so an experiment can run even if a fixture
        has been removed — the structural skip is flagged by the
        runner when ``cases`` ends up empty.
    """
    manifest = load_manifest()
    by_id = {entry.id: entry for entry in manifest.entries}

    cases: list[Case[dict[str, Any], dict[str, Any]]] = []

    for fixture_id, mode in _FAILURE_MODE_FIXTURES.items():
        entry = by_id.get(fixture_id)
        if entry is None:
            continue
        cases.append(_case_for_fixture(entry, expected_failure_mode=mode))

    for fixture_id in _CLEAN_CONTROL_FIXTURES:
        entry = by_id.get(fixture_id)
        if entry is None:
            continue
        cases.append(_case_for_fixture(entry, expected_failure_mode="clean"))

    return cases


def build_failure_mode_video_experiment() -> Experiment[dict[str, Any], dict[str, Any]]:
    """Construct the failure-mode :class:`Experiment`.

    Raises:
        ValueError: If no cases could be built (no fixtures present).
            The runner turns this into an ``EXIT_SKIP`` so CI reports
            a neutral yellow rather than a false-positive green.
    """
    if shutil.which("ffmpeg") is None:
        raise ValueError(
            "ffmpeg not found on PATH — failure-mode video experiment "
            "requires ffmpeg for pixel decoding"
        )
    cases = build_cases()
    if not cases:
        raise ValueError(
            "no fixtures available for failure-mode video experiment; "
            "check that fixtures/manifest.json and the committed mp4s "
            "are present"
        )
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=cases,
        evaluators=[FailureModeEvaluator()],
    )


def _signals_to_dict(signals: VideoSignals) -> dict[str, Any]:
    """Render :class:`VideoSignals` as a JSON-friendly dict."""
    return asdict(signals)


def failure_mode_video_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Task function for :func:`Experiment.run_evaluations`.

    Resolves the fixture path from the :class:`Case` input, runs the
    full detector stack, and returns the signals envelope expected
    by :class:`FailureModeEvaluator`.

    Args:
        case: One case from :func:`build_cases`.

    Returns:
        ``{"output": {"local_path": str, "signals": {...}}}``. The
        framework stores the inner dict as
        :attr:`EvaluationData.actual_output`.
    """
    fixture_input = case.input or {}
    stub = FixtureEntry(
        id=str(fixture_input.get("id", "")),
        axis=str(fixture_input.get("axis", "")),
        media=str(fixture_input.get("media", "")),
        relative_path=str(fixture_input.get("relative_path", "")),
        sha256=str(fixture_input.get("sha256", "")),
        expected_verdict=str(fixture_input.get("expected_verdict", "")),
        prompt=str(fixture_input.get("prompt", "")),
        public_url=fixture_input.get("public_url"),
    )
    local_path: Path = resolve_fixture_path(stub)
    signals = extract_signals(local_path)
    return {
        "output": {
            "local_path": str(local_path),
            "signals": _signals_to_dict(signals),
        }
    }


__all__ = [
    "build_cases",
    "build_failure_mode_video_experiment",
    "failure_mode_video_task",
]


if __name__ == "__main__":
    sys.exit(
        run_experiment_as_main(
            build_failure_mode_video_experiment,
            failure_mode_video_task,
            name="failure_mode_video",
        )
    )
