"""Pair-discrimination Experiment on top of the media corpus.

Every pair is a matched positive/negative on one clear-cut axis:
``HELLO`` vs ``GOODBYE`` for ``text_present_hello``, solid-red vs
solid-green for ``color_red``, and so on. A judge that returns the
same answer for both sides of a pair has failed to discriminate and
is a candidate for discard — that's the signal this Experiment is
designed to surface.

Shape
-----

* The :class:`Experiment` is the same shape as the per-fixture one in
  :mod:`media_corpus` — one :class:`Case` per fixture — but the
  fixture set is restricted to the closed pair cover: every axis that
  has both a ``yes`` and a ``no`` fixture is enrolled, everything
  else is left out. Singleton axes would give a false pass on pair
  discrimination.
* Grading happens in :class:`LiveMediaJudgeEvaluator` exactly as in
  :mod:`media_corpus`. The pair structure is a filter on which cases
  run, not a new evaluator.
* :func:`summarize_pair_discrimination` post-processes the
  :class:`EvaluationReport` returned by
  :func:`Experiment.run_evaluations` and reports per-axis whether the
  judge answered both sides correctly (``both_correct``), confused
  them (``same_answer``), or only got one side right
  (``one_correct``). Callers use the summary in assertions — it's
  not a substitute for per-case grading, it's a second lens on the
  same answers.

No new fixture bytes are added here; the corpus itself lives in
``fixtures/manifest.json`` and is curated to keep every axis paired.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from strands_evals.case import Case
from strands_evals.experiment import Experiment
from strands_evals.types.evaluation_report import EvaluationReport

from strands_agents.evals.evaluators import LiveMediaJudgeEvaluator
from strands_agents.evals.fixtures.manifest import (
    FixtureEntry,
    load_manifest,
)

from .media_corpus import _case_for_fixture

_BINARY_VERDICTS = frozenset({"yes", "no"})


def _closed_pair_axes(entries: tuple[FixtureEntry, ...], media: str) -> set[str]:
    """Return the set of axes that have both a ``yes`` and a ``no`` fixture.

    Singleton axes (one-sided) are excluded. Axes with only
    ``reject`` fixtures are excluded because reject fixtures are not
    judge-graded.
    """
    by_axis: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        if entry.media != media:
            continue
        if entry.expected_verdict not in _BINARY_VERDICTS:
            continue
        by_axis[entry.axis].add(entry.expected_verdict)
    return {axis for axis, verdicts in by_axis.items() if verdicts == _BINARY_VERDICTS}


def build_pair_cases(*, media: str) -> list[Case[dict[str, Any], str]]:
    """Build one :class:`Case` per fixture on every closed-pair axis.

    Args:
        media: ``"video"`` or ``"audis"`` — filters manifest before
            pairing.

    Returns:
        Cases for every fixture whose axis has both a ``yes`` and a
        ``no`` sibling. Empty list if no pairs exist for the media
        kind.
    """
    manifest = load_manifest()
    paired = _closed_pair_axes(manifest.entries, media)
    return [
        _case_for_fixture(entry)
        for entry in manifest.entries
        if entry.media == media
        and entry.axis in paired
        and entry.expected_verdict in _BINARY_VERDICTS
    ]


def build_pair_experiment(
    *,
    media: str,
) -> Experiment[dict[str, Any], str]:
    """Construct the pair-discrimination :class:`Experiment`.

    Fails fast with :class:`ValueError` if there are no closed pairs
    for the requested media kind — a pair Experiment with no pairs
    would silently pass and hide the real problem.
    """
    cases = build_pair_cases(media=media)
    if not cases:
        raise ValueError(
            f"no closed pair axes for media={media!r}; "
            "add matching yes/no fixtures to the manifest before "
            "building a pair experiment"
        )
    return Experiment[dict[str, Any], str](
        cases=cases,
        evaluators=[LiveMediaJudgeEvaluator()],
    )


# ---------------------------------------------------------------------------
# Post-processing: turn one EvaluationReport into per-axis discrimination.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairOutcome:
    """Per-axis discrimination outcome.

    Attributes:
        axis: The axis id (``"text_present_hello"`` etc.).
        yes_passed: True iff the positive side case (expected_output
            ``"yes"``) had every non-skipped judge output pass.
        no_passed: True iff the negative side case (expected_output
            ``"no"``) had every non-skipped judge output pass.
        yes_case: Case name for the positive side (``fixture.<id>``).
        no_case: Case name for the negative side.
        verdict: One of ``"both_correct"``, ``"same_answer"``,
            ``"flipped"``, ``"unclear"``.

            * ``both_correct`` — judge passed both sides.
            * ``same_answer`` — judge gave the same binary answer to
              both sides of the pair (the pure discrimination
              failure; the classic "always says yes" / "always says
              no" bug).
            * ``flipped`` — judge gave opposite answers on the two
              sides but both were wrong (said ``no`` to the positive
              and ``yes`` to the negative). Rare, still a failure,
              but the judge *can* discriminate — it's just backwards.
            * ``unclear`` — at least one side's judge answer could
              not be parsed (all skipped, judges tied, empty
              outputs).
    """

    axis: str
    yes_passed: bool
    no_passed: bool
    yes_case: str
    no_case: str
    verdict: str


@dataclass(frozen=True)
class PairSummary:
    """Aggregate discrimination summary across all axes in a report."""

    outcomes: tuple[PairOutcome, ...]

    @property
    def all_discriminated(self) -> bool:
        """True iff every axis returned ``both_correct``."""
        return all(outcome.verdict == "both_correct" for outcome in self.outcomes)

    @property
    def failures(self) -> tuple[PairOutcome, ...]:
        """Every outcome whose verdict is not ``both_correct``."""
        return tuple(o for o in self.outcomes if o.verdict != "both_correct")


def summarize_pair_discrimination(report: EvaluationReport) -> PairSummary:
    """Reduce a :class:`EvaluationReport` into per-axis pair outcomes.

    The report is expected to come from an Experiment built by
    :func:`build_pair_experiment` — cases tagged with a pair-axis, one
    yes-side and one no-side per axis.

    Skipped judge outputs (``judge.skipped.*`` labels) are tolerated
    because they mean no judge actually answered; a pair that is
    half-skipped is not a discrimination failure, it's an
    incomplete run.

    Args:
        report: The report returned by
            :func:`Experiment.run_evaluations`.

    Returns:
        A :class:`PairSummary` with one :class:`PairOutcome` per axis.
    """
    cases = report.cases or []
    by_axis: dict[str, dict[str, _SideState]] = defaultdict(dict)
    for idx, case in enumerate(cases):
        meta = case.get("metadata") or {}
        axis = str(meta.get("axis") or "unknown")
        expected = (case.get("expected_output") or "").strip().lower()
        case_name = str(case.get("name") or f"case[{idx}]")
        if expected not in _BINARY_VERDICTS:
            continue
        side_outputs = (
            report.detailed_results[idx]
            if idx < len(report.detailed_results)
            else []
        )
        by_axis[axis][expected] = _side_state(case_name, side_outputs)

    outcomes: list[PairOutcome] = []
    for axis in sorted(by_axis):
        sides = by_axis[axis]
        yes_side = sides.get("yes", _missing_side("missing_yes"))
        no_side = sides.get("no", _missing_side("missing_no"))
        outcomes.append(
            PairOutcome(
                axis=axis,
                yes_passed=yes_side.passed,
                no_passed=no_side.passed,
                yes_case=yes_side.case_name,
                no_case=no_side.case_name,
                verdict=_verdict_for(yes_side, no_side),
            )
        )
    return PairSummary(outcomes=tuple(outcomes))


@dataclass(frozen=True)
class _SideState:
    """Per-side grading state for one fixture in a pair."""

    case_name: str
    passed: bool
    answer: str | None


def _missing_side(case_name: str) -> _SideState:
    return _SideState(case_name=case_name, passed=False, answer=None)


def _side_state(case_name: str, side_outputs: list[Any]) -> _SideState:
    """Collapse one side's evaluator outputs into pass/answer."""
    hard_failed = any(
        not getattr(out, "test_pass", True)
        and not (getattr(out, "label", "") or "").startswith("judge.skipped.")
        for out in side_outputs
    )
    answer = _dominant_judge_answer(side_outputs)
    return _SideState(case_name=case_name, passed=not hard_failed, answer=answer)


def _dominant_judge_answer(side_outputs: list[Any]) -> str | None:
    """Extract the judge's binary answer for a side, or ``None`` if unclear.

    Scans per-model judge outputs (labels of the form
    ``judge.<model>.<axis>``) for the ``answered yes`` / ``answered no``
    phrase emitted by :func:`_grade`. The reason string has shape
    ``"<model> answered <y/n>; expected <y/n>; raw=<up-to-120-char
    model output>"``. Only the prefix before ``"; raw="`` is scanned,
    because the raw model output can itself contain the
    ``"answered yes"`` / ``"answered no"`` substrings and would
    otherwise poison the match.

    Returns the majority answer if judges disagree, or ``None`` if no
    answer could be parsed (all skipped, empty report, unrecognised
    label, tied judges).
    """
    yes = 0
    no = 0
    for out in side_outputs:
        label = getattr(out, "label", "") or ""
        if not label.startswith("judge."):
            continue
        if label.startswith("judge.consensus.") or label.startswith("judge.skipped."):
            continue
        reason = (getattr(out, "reason", "") or "").lower()
        # Strip the ``; raw=…`` tail before scanning — it's the raw
        # model output and can contain the same answer phrases.
        prefix = reason.split("; raw=", 1)[0]
        if "answered yes" in prefix:
            yes += 1
        elif "answered no" in prefix:
            no += 1
    if yes == 0 and no == 0:
        return None
    if yes > no:
        return "yes"
    if no > yes:
        return "no"
    return None  # tie: judges disagree with each other, not a clean answer


def _verdict_for(yes_side: _SideState, no_side: _SideState) -> str:
    """Classify a pair outcome.

    See :class:`PairOutcome` for the full semantics.
    """
    if yes_side.passed and no_side.passed:
        return "both_correct"
    if yes_side.answer is None or no_side.answer is None:
        return "unclear"
    if yes_side.answer == no_side.answer:
        return "same_answer"
    return "flipped"
