"""Media-corpus Experiment — judges the committed fixtures live.

One :class:`Experiment` per media kind (video, audio). Each case
points at one fixture in ``fixtures/manifest.json`` and carries the
clear-cut expected verdict. :class:`LiveMediaJudgeEvaluator` calls
the production judge stack and grades the answer.

Scope for PR-M2
---------------
* Only ``expected_verdict in {"yes", "no"}`` fixtures are enrolled.
  ``reject`` fixtures exist but are graded by deterministic QA gates
  (frozen-frame, black-frame, LUFS), not by LLM judges.
* Judge routing is Gemini-everywhere + Qwen-when-public-URL. Audio is
  Gemini-only today.
* The task function in this module is minimal — it translates a
  manifest entry into the ``{"output": {...}}`` shape the evaluator
  expects. Real judging happens in the evaluator.
"""

from __future__ import annotations

import sys
from typing import Any

from strands_evals.case import Case
from strands_evals.experiment import Experiment

from strands_agents.evals._runner import run_experiment_as_main
from strands_agents.evals.evaluators import LiveMediaJudgeEvaluator
from strands_agents.evals.fixtures.manifest import (
    FixtureEntry,
    load_manifest,
    resolve_fixture_path,
)

_BINARY_VERDICTS = frozenset({"yes", "no"})


def _fixture_to_input(entry: FixtureEntry) -> dict[str, Any]:
    """Pack a :class:`FixtureEntry` into a case ``input`` dict.

    The case input is copied verbatim into :class:`EvaluationData.input`,
    so it carries enough context for the evaluator to form a prompt
    (``prompt``, ``axis``) and for diagnostics (``id``, ``sha256``).
    """
    return {
        "id": entry.id,
        "axis": entry.axis,
        "media": entry.media,
        "relative_path": entry.relative_path,
        "sha256": entry.sha256,
        "prompt": entry.prompt,
        "expected_verdict": entry.expected_verdict,
        "public_url": entry.public_url,
    }


def _case_for_fixture(entry: FixtureEntry) -> Case[dict[str, Any], str]:
    """Build a single :class:`Case` pointing at one fixture."""
    return Case[dict[str, Any], str](
        name=f"fixture.{entry.id}",
        input=_fixture_to_input(entry),
        expected_output=entry.expected_verdict,
        metadata={
            "fixture_id": entry.id,
            "axis": entry.axis,
            "media": entry.media,
            "sha256": entry.sha256,
        },
    )


def build_cases(
    *,
    media: str,
    include_verdicts: frozenset[str] = _BINARY_VERDICTS,
) -> list[Case[dict[str, Any], str]]:
    """Build the list of cases for a given media kind.

    Args:
        media: ``"video"`` or ``"audio"``. Filters the manifest to one
            media type per experiment so failures are easier to
            attribute.
        include_verdicts: Verdicts to enroll. Defaults to the binary
            pair — callers can pass a narrower set to focus a run.

    Returns:
        One :class:`Case` per matching fixture, in manifest order.
    """
    manifest = load_manifest()
    return [
        _case_for_fixture(entry)
        for entry in manifest.entries
        if entry.media == media and entry.expected_verdict in include_verdicts
    ]


def build_media_corpus_experiment(
    *,
    media: str,
    include_verdicts: frozenset[str] = _BINARY_VERDICTS,
) -> Experiment[dict[str, Any], str]:
    """Construct the media-corpus :class:`Experiment`.

    Args:
        media: ``"video"`` or ``"audio"``.
        include_verdicts: Verdicts to enroll. Defaults to binary
            yes/no — judges grade those; deterministic QA evaluators
            grade ``reject`` fixtures in a separate Experiment.

    Returns:
        An :class:`Experiment` wired with one
        :class:`LiveMediaJudgeEvaluator` and the filtered cases.
    """
    return Experiment[dict[str, Any], str](
        cases=build_cases(media=media, include_verdicts=include_verdicts),
        evaluators=[LiveMediaJudgeEvaluator()],
    )


def media_task(case: Case[dict[str, Any], str]) -> dict[str, Any]:
    """Task function for :func:`Experiment.run_evaluations`.

    Resolves the fixture's local path from the manifest entry and
    returns the ``{"output": {...}}`` envelope strands_evals expects.
    No actual judging happens here — the evaluator calls out to the
    production judge stack.

    Args:
        case: One case from :func:`build_cases`.

    Returns:
        Dict with an ``output`` key containing the local path, public
        URL (may be None), and media kind. strands_evals sets that
        dict as :class:`EvaluationData.actual_output`.
    """
    fixture_input = case.input or {}
    # Rehydrate the :class:`FixtureEntry` enough to resolve the path
    # without re-reading the manifest. The case was built from a real
    # entry, so ``relative_path`` is trustable here.
    from strands_agents.evals.fixtures.manifest import FixtureEntry

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
    local_path = resolve_fixture_path(stub)
    return {
        "output": {
            "local_path": str(local_path),
            "public_url": stub.public_url,
            "media": stub.media,
        }
    }


__all__ = [
    "build_cases",
    "build_media_corpus_experiment",
    "media_task",
]


if __name__ == "__main__":
    # Default to the video corpus; audio can be invoked explicitly
    # via ``MEDIA_CORPUS_MEDIA=audio python -m
    # strands_agents.evals.experiments.media_corpus``. No CLI parser
    # by design — the idiom is ``python -m <module>``, with env vars
    # for the one or two knobs that differ between runs.
    import os as _os

    _media = _os.environ.get("MEDIA_CORPUS_MEDIA", "video")
    sys.exit(
        run_experiment_as_main(
            lambda: build_media_corpus_experiment(media=_media),
            media_task,
            name=f"media_corpus.{_media}",
        )
    )
