"""A/V desync Experiment — assembly-layer signal-level gate.

Runs :class:`AVDesyncEvaluator` over the five committed combined
audio+video fixtures, each carrying an explicit
``expected_desync_mode`` classification. All grading is offline and
deterministic — ffmpeg/ffprobe decode streams into numpy arrays,
numpy computes onsets, no LLM or external service is touched.

This experiment is the assembly-layer counterpart to
:mod:`failure_mode_video` (video-only) and
:mod:`audio_failure_mode` (audio-only). The signal primitives live
one layer deeper than either — they ask "do the two rails line up
at all", not "is this rail alone clean".

Invocation
----------
Idiomatic ``python -m`` entrypoint::

    python -m strands_agents.evals.experiments.av_desync

Exit codes follow the repo-wide runner contract: ``0`` on full
green, ``1`` on any failure, ``2`` on a structural skip (no
fixtures present). CI invokes the module as a matrix cell in
``.github/workflows/strands-evals.yml``.
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from strands_evals.case import Case
from strands_evals.experiment import Experiment

from strands_agents.evals._runner import run_experiment_as_main
from strands_agents.evals.evaluators import AVDesyncEvaluator
from strands_agents.evals.evaluators.av_desync_detectors import (
    AVSignals,
    extract_av_signals,
)
from strands_agents.evals.fixtures.manifest import (
    FixtureEntry,
    load_manifest,
    resolve_fixture_path,
)

# Which fixtures map to which desync mode. Kept explicit so a
# fixture with a weirdly-named axis can be added or relabelled
# without silently mis-classifying the evaluator's gate.
_DESYNC_MODE_FIXTURES: dict[str, str] = {
    "av_synced_narration": "synced",
    "av_audio_ahead": "audio_ahead",
    "av_audio_behind": "audio_behind",
    "av_audio_missing": "audio_missing",
    "av_video_missing": "video_missing",
}


def _case_for_fixture(
    entry: FixtureEntry,
    *,
    expected_desync_mode: str,
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
        name=f"av_desync.{expected_desync_mode}.{entry.id}",
        input=fixture_input,
        expected_output={"desync_mode": expected_desync_mode},
        metadata={
            "fixture_id": entry.id,
            "axis": entry.axis,
            "media": entry.media,
            "sha256": entry.sha256,
            "expected_desync_mode": expected_desync_mode,
        },
    )


def build_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Build the list of cases from the committed manifest.

    Returns:
        One :class:`Case` per AV fixture declared in
        :data:`_DESYNC_MODE_FIXTURES`. Silently skips any fixture
        id not present in the manifest so the experiment can run
        even if a fixture has been removed — the structural skip
        is flagged by the runner when ``cases`` ends up empty.
    """
    manifest = load_manifest()
    by_id = {entry.id: entry for entry in manifest.entries}

    cases: list[Case[dict[str, Any], dict[str, Any]]] = []
    for fixture_id, mode in _DESYNC_MODE_FIXTURES.items():
        entry = by_id.get(fixture_id)
        if entry is None:
            continue
        cases.append(_case_for_fixture(entry, expected_desync_mode=mode))
    return cases


def build_av_desync_experiment() -> Experiment[dict[str, Any], dict[str, Any]]:
    """Construct the A/V desync :class:`Experiment`.

    Pure factory: composes cases and wires the evaluator without
    touching the filesystem, so unit tests can call it in
    isolation. The ffmpeg decode path (which does need the
    fixtures on disk) runs inside :func:`av_desync_task` when the
    experiment is actually executed.

    Raises:
        ValueError: If no cases could be built (no fixtures
            present). The runner turns this into an ``EXIT_SKIP``
            so CI reports a neutral yellow rather than a
            false-positive green.
    """
    cases = build_cases()
    if not cases:
        raise ValueError(
            "no fixtures available for A/V desync experiment; "
            "check that fixtures/manifest.json and the committed "
            "av/*.mp4 files are present"
        )
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=cases,
        evaluators=[AVDesyncEvaluator()],
    )


def _signals_to_dict(signals: AVSignals) -> dict[str, Any]:
    """Render :class:`AVSignals` as a JSON-friendly dict."""
    return asdict(signals)


def av_desync_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Task function for :func:`Experiment.run_evaluations`.

    Resolves the fixture path from the :class:`Case` input, runs
    the full detector stack, and returns the signals envelope
    expected by :class:`AVDesyncEvaluator`.

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
    signals = extract_av_signals(local_path)
    return {
        "output": {
            "local_path": str(local_path),
            "signals": _signals_to_dict(signals),
        }
    }


__all__ = [
    "av_desync_task",
    "build_av_desync_experiment",
    "build_cases",
]


if __name__ == "__main__":
    sys.exit(
        run_experiment_as_main(
            build_av_desync_experiment,
            av_desync_task,
            name="av_desync",
        )
    )
