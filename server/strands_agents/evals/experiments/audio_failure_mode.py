"""Failure-mode audio Experiment — deterministic QA gate corpus.

Runs :class:`AudioFailureModeEvaluator` over the committed audio
fixtures that carry an explicit ``expected_failure_mode``
classification, plus a set of clean positive controls. All grading
is offline and deterministic — the stdlib :mod:`wave` module
decodes PCM, numpy runs the detectors, no LLM or external service
is touched.

This is the audio counterpart to :mod:`failure_mode_video`. The
failure-mode half of the audio corpus is graded here (by signal
primitives) because clear-cut reject classes — pure silence,
pure noise, hard clipping — are cheaper and more reliable to
flag with numbers than with a judge.

Invocation
----------
Idiomatic ``python -m`` entrypoint::

    python -m strands_agents.evals.experiments.audio_failure_mode

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
from strands_agents.evals.evaluators import AudioFailureModeEvaluator
from strands_agents.evals.evaluators.audio_failure_mode_detectors import (
    AudioSignals,
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
    "audio_silence": "silence",
    "audio_noise_only": "noise",
    "audio_clipping": "clipping",
}

# Positive controls — clean narration clips that must NOT trip any
# detector. The Spanish narration is deliberately included: from
# the signal-level QA perspective it is a perfectly clean voice
# recording; its "wrong language" classification lives in the
# judge-facing corpus, not here.
_CLEAN_CONTROL_FIXTURES: tuple[str, ...] = (
    "audio_hello_narration",
    "audio_english_narration",
    "audio_spanish_narration",
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
        name=f"audio_failure_mode.{expected_failure_mode}.{entry.id}",
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
        in the manifest so the experiment can run even if a fixture
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


def build_audio_failure_mode_experiment() -> Experiment[dict[str, Any], dict[str, Any]]:
    """Construct the audio failure-mode :class:`Experiment`.

    Pure factory: it composes cases and wires the evaluator without
    touching the filesystem, so unit tests can call it in
    isolation. The WAV decode path (which does need the fixtures
    on disk) runs inside :func:`audio_failure_mode_task` when the
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
            "no fixtures available for audio failure-mode experiment; "
            "check that fixtures/manifest.json and the committed wavs "
            "are present"
        )
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=cases,
        evaluators=[AudioFailureModeEvaluator()],
    )


def _signals_to_dict(signals: AudioSignals) -> dict[str, Any]:
    """Render :class:`AudioSignals` as a JSON-friendly dict."""
    return asdict(signals)


def audio_failure_mode_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Task function for :func:`Experiment.run_evaluations`.

    Resolves the fixture path from the :class:`Case` input, runs
    the full detector stack, and returns the signals envelope
    expected by :class:`AudioFailureModeEvaluator`.

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
    "audio_failure_mode_task",
    "build_audio_failure_mode_experiment",
    "build_cases",
]


if __name__ == "__main__":
    sys.exit(
        run_experiment_as_main(
            build_audio_failure_mode_experiment,
            audio_failure_mode_task,
            name="audio_failure_mode",
        )
    )
