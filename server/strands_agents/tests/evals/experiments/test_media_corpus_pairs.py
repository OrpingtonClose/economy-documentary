"""Tests for the pair-discrimination media Experiment.

Two layers:

* **Offline tests** — build the pair Experiment, swap the judge for a
  deterministic oracle, and verify the per-axis discrimination
  summary correctly classifies each axis as
  ``both_correct`` / ``one_correct`` / ``same_answer`` under pinned
  judge behaviour. Proves the post-processing reducer is wired
  correctly without any network.
* **Live tests** — when ``GOOGLE_API_KEY`` is set, run the pair
  Experiment against the production judges and assert every axis
  returns ``both_correct``. A single ``same_answer`` or
  ``one_correct`` is a judge-discrimination failure and surfaces as
  a test failure.
"""

from __future__ import annotations

import os

import pytest

from strands_agents.evals.evaluators.live_media_judge import (
    LiveMediaJudgeEvaluator,
    _JudgeResult,
)
from strands_agents.evals.experiments.media_corpus import media_task
from strands_agents.evals.experiments.media_corpus_pairs import (
    _dominant_judge_answer,
    build_pair_cases,
    build_pair_experiment,
    summarize_pair_discrimination,
)
from strands_evals.types.evaluation import EvaluationOutput
from strands_agents.evals.fixtures.manifest import load_manifest


# ---------------------------------------------------------------------------
# Offline — oracle, liar, half-correct judges against the pair Experiment.
# ---------------------------------------------------------------------------

_VIDEO_YES_FILENAMES: frozenset[str] = frozenset(
    entry.relative_path.split("/")[-1]
    for entry in load_manifest().entries
    if entry.media == "video" and entry.expected_verdict == "yes"
)


def _oracle_video_judge(
    local_path: str, public_url: str | None, prompt: str
) -> tuple[_JudgeResult, ...]:
    """Deterministic judge that answers every fixture correctly."""
    name = os.path.basename(local_path)
    yes = name in _VIDEO_YES_FILENAMES
    gemini = _JudgeResult(
        model="gemini-2.5-flash",
        ran=True,
        judged_yes=yes,
        raw_text="yes" if yes else "no",
    )
    if public_url:
        qwen = _JudgeResult(
            model="qwen-vl-plus",
            ran=True,
            judged_yes=yes,
            raw_text="yes" if yes else "no",
        )
        return (gemini, qwen)
    return (gemini,)


def _liar_video_judge(
    local_path: str, public_url: str | None, prompt: str
) -> tuple[_JudgeResult, ...]:
    """Always answers ``yes``. Passes every positive, fails every negative."""
    gemini = _JudgeResult(
        model="gemini-2.5-flash",
        ran=True,
        judged_yes=True,
        raw_text="yes",
    )
    if public_url:
        qwen = _JudgeResult(
            model="qwen-vl-plus",
            ran=True,
            judged_yes=True,
            raw_text="yes",
        )
        return (gemini, qwen)
    return (gemini,)


def _flipping_video_judge(
    local_path: str, public_url: str | None, prompt: str
) -> tuple[_JudgeResult, ...]:
    """Flips both sides of the ``color_red`` axis; other axes correct.

    On ``color_red`` the judge inverts both answers — says ``no`` to
    the red fixture and ``yes`` to the green one. That is the
    ``flipped`` verdict: the judge is discriminating (giving
    different answers to the two sides), but backwards.
    """
    name = os.path.basename(local_path)
    if name == "video_solid_red.mp4":
        yes = False  # correct is yes; flip to no
    elif name == "video_solid_green.mp4":
        yes = True  # correct is no; flip to yes
    else:
        yes = name in _VIDEO_YES_FILENAMES
    gemini = _JudgeResult(
        model="gemini-2.5-flash",
        ran=True,
        judged_yes=yes,
        raw_text="yes" if yes else "no",
    )
    if public_url:
        qwen = _JudgeResult(
            model="qwen-vl-plus",
            ran=True,
            judged_yes=yes,
            raw_text="yes" if yes else "no",
        )
        return (gemini, qwen)
    return (gemini,)


def test_dominant_judge_answer_ignores_raw_output_poison() -> None:
    """Poisoned raw model output must not be mistaken for the judge's answer.

    ``_grade`` emits ``reason`` strings of shape
    ``"<model> answered no; expected yes; raw='…answered yes…'"``.
    A naive substring scan would see the ``answered yes`` inside the
    raw tail and flip the parsed answer. The parser must restrict its
    scan to the prefix before ``"; raw="``.
    """
    poisoned = EvaluationOutput(
        score=0.0,
        test_pass=False,
        reason=(
            "gemini-2.5-flash answered no; expected yes; "
            "raw='yes the model clearly answered yes to this question'"
        ),
        label="judge.gemini-2.5-flash.color_red",
    )
    assert _dominant_judge_answer([poisoned]) == "no"

    # Mirror case: prefix says yes, raw tail carries a "no" phrase.
    poisoned_yes = EvaluationOutput(
        score=1.0,
        test_pass=True,
        reason=(
            "qwen-vl-plus answered yes; expected yes; "
            "raw='the correct response would have answered no historically'"
        ),
        label="judge.qwen-vl-plus.color_red",
    )
    assert _dominant_judge_answer([poisoned_yes]) == "yes"


def test_dominant_judge_answer_skips_consensus_and_skipped_labels() -> None:
    """Aggregate labels must be ignored — only per-model entries vote."""
    per_model_yes = EvaluationOutput(
        score=1.0,
        test_pass=True,
        reason="gemini-2.5-flash answered yes; expected yes; raw='yes'",
        label="judge.gemini-2.5-flash.x",
    )
    consensus = EvaluationOutput(
        score=0.0,
        test_pass=False,
        reason="judges disagree with each other or with expected verdict",
        label="judge.consensus.x",
    )
    skipped = EvaluationOutput(
        score=0.0,
        test_pass=False,
        reason="no judges available",
        label="judge.skipped.x",
    )
    # Only the per-model entry should count; the other two contain no
    # ``answered yes/no`` phrase anyway but we check the prefix filter
    # directly.
    assert _dominant_judge_answer([per_model_yes, consensus, skipped]) == "yes"


def test_dominant_judge_answer_returns_none_on_empty_or_tie() -> None:
    """No parseable answers → ``None``; 1-yes-1-no tie → ``None``."""
    assert _dominant_judge_answer([]) is None

    unrelated = EvaluationOutput(
        score=0.0, test_pass=False, reason="totally unrelated text", label="judge.x.y"
    )
    assert _dominant_judge_answer([unrelated]) is None

    yes_out = EvaluationOutput(
        score=1.0,
        test_pass=True,
        reason="a answered yes; expected yes; raw='yes'",
        label="judge.a.x",
    )
    no_out = EvaluationOutput(
        score=0.0,
        test_pass=False,
        reason="b answered no; expected yes; raw='no'",
        label="judge.b.x",
    )
    assert _dominant_judge_answer([yes_out, no_out]) is None


def test_pair_cases_are_closed_pairs() -> None:
    """Every axis in the pair corpus has exactly one yes and one no."""
    cases = build_pair_cases(media="video")
    by_axis: dict[str, list[str]] = {}
    for case in cases:
        axis = case.metadata.get("axis")
        assert axis, f"case {case.name} missing axis metadata"
        assert case.expected_output in {"yes", "no"}
        by_axis.setdefault(axis, []).append(case.expected_output)

    assert by_axis, "expected at least one pair axis"
    for axis, verdicts in by_axis.items():
        assert sorted(verdicts) == ["no", "yes"], (
            f"axis {axis!r} is not a closed pair: verdicts={verdicts}"
        )


def test_build_pair_experiment_fails_on_empty_corpus(monkeypatch) -> None:
    """The factory must refuse to build a pair Experiment with no pairs."""
    import strands_agents.evals.experiments.media_corpus_pairs as mod

    monkeypatch.setattr(mod, "load_manifest", lambda: _EmptyManifest())
    with pytest.raises(ValueError, match="no closed pair axes"):
        build_pair_experiment(media="video")


class _EmptyManifest:
    """Stand-in manifest with no entries."""

    entries: tuple = ()


def test_pair_experiment_oracle_all_both_correct() -> None:
    """Oracle judge: every axis resolves to ``both_correct``."""
    evaluator = LiveMediaJudgeEvaluator(video_judge=_oracle_video_judge)
    cases = build_pair_cases(media="video")

    from strands_evals.experiment import Experiment

    experiment = Experiment(cases=cases, evaluators=[evaluator])
    reports = experiment.run_evaluations(task=media_task)
    assert len(reports) == 1

    summary = summarize_pair_discrimination(reports[0])

    assert summary.outcomes, "summary must contain at least one pair"
    assert summary.all_discriminated, (
        f"oracle judge should discriminate every pair, got failures: "
        f"{summary.failures}"
    )
    assert all(o.verdict == "both_correct" for o in summary.outcomes)


def test_pair_experiment_liar_all_same_answer() -> None:
    """A judge that always says yes fails every negative case.

    Every axis must resolve to ``same_answer`` — the yes-side passes
    (expected and got ``yes``) but the no-side fails (expected
    ``no``, got ``yes``). ``same_answer`` means the judge gave the
    same answer to both sides; here that's ``yes`` everywhere.
    """
    evaluator = LiveMediaJudgeEvaluator(video_judge=_liar_video_judge)
    cases = build_pair_cases(media="video")

    from strands_evals.experiment import Experiment

    experiment = Experiment(cases=cases, evaluators=[evaluator])
    reports = experiment.run_evaluations(task=media_task)

    summary = summarize_pair_discrimination(reports[0])
    assert summary.outcomes
    assert not summary.all_discriminated
    # Every axis should land in ``same_answer``: liar answers yes
    # everywhere, so the yes-side passes and the no-side fails —
    # which means the judge's answer was identical on both sides of
    # every pair. That is exactly ``same_answer``.
    assert all(o.verdict == "same_answer" for o in summary.outcomes), (
        f"liar judge must collapse every pair to same_answer, got "
        f"{[(o.axis, o.verdict) for o in summary.outcomes]}"
    )


def test_pair_experiment_flipper_surfaces_flipped() -> None:
    """Inverting both sides of one axis produces the ``flipped`` verdict.

    The judge gives different answers on the two sides — it *can*
    discriminate — but backwards. The summary must surface this
    distinctly from ``same_answer`` because it tells the caller the
    judge has signal, just reversed.
    """
    evaluator = LiveMediaJudgeEvaluator(video_judge=_flipping_video_judge)
    cases = build_pair_cases(media="video")

    from strands_evals.experiment import Experiment

    experiment = Experiment(cases=cases, evaluators=[evaluator])
    reports = experiment.run_evaluations(task=media_task)

    summary = summarize_pair_discrimination(reports[0])
    failures = [o for o in summary.outcomes if o.verdict != "both_correct"]
    assert len(failures) == 1, (
        f"flipper judge should fail exactly one axis, got "
        f"{[(o.axis, o.verdict) for o in failures]}"
    )
    (bad,) = failures
    assert bad.axis == "color_red"
    assert bad.verdict == "flipped"
    assert bad.yes_passed is False
    assert bad.no_passed is False


def test_pair_summary_handles_missing_sides_gracefully() -> None:
    """If a report only contains one half of a pair, the missing side is flagged."""
    evaluator = LiveMediaJudgeEvaluator(video_judge=_oracle_video_judge)
    cases = [c for c in build_pair_cases(media="video") if c.expected_output == "yes"]

    from strands_evals.experiment import Experiment

    experiment = Experiment(cases=cases, evaluators=[evaluator])
    reports = experiment.run_evaluations(task=media_task)

    summary = summarize_pair_discrimination(reports[0])
    assert summary.outcomes
    for outcome in summary.outcomes:
        assert outcome.yes_passed is True
        assert outcome.no_passed is False
        # No-side never ran, so its answer is None → verdict unclear.
        assert outcome.verdict == "unclear"
        assert outcome.no_case.startswith("missing_"), (
            f"expected sentinel for missing side, got {outcome.no_case!r}"
        )


# ---------------------------------------------------------------------------
# Live judges — real API calls. Skipped without credentials.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.getenv("GOOGLE_API_KEY"),
    reason="GOOGLE_API_KEY not set; live Gemini judge unavailable",
)
def test_live_pair_discrimination_video() -> None:
    """Real Gemini must discriminate every paired axis in the video corpus.

    A ``same_answer`` or ``one_correct`` for any axis means the model
    failed a clear-cut binary discrimination — per the gate rule,
    that's a candidate-for-discard signal.
    """
    experiment = build_pair_experiment(media="video")
    reports = experiment.run_evaluations(task=media_task)
    summary = summarize_pair_discrimination(reports[0])
    failed = summary.failures
    assert not failed, (
        "live Gemini failed to discriminate pairs: "
        + ", ".join(
            f"{o.axis}({o.verdict}: yes={o.yes_passed}, no={o.no_passed})"
            for o in failed
        )
    )
