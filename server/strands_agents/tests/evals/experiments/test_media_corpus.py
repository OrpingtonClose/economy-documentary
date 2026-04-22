"""Tests for the media-corpus Experiment.

Two layers:

* **Offline unit tests** — build the Experiment, swap the judge
  callbacks for deterministic fakes, and exercise the full
  ``run_evaluations`` lifecycle. Proves the Experiment wiring
  (cases, task, evaluator, report aggregation) is correct without
  any network.
* **Live judge tests** — when ``GOOGLE_API_KEY`` (and optionally
  ``DASHSCOPE_API_KEY``) is set, run the same Experiment against the
  production judges and assert every case passes. Skipped in CI by
  default; run locally or in the nightly live workflow.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from strands_evals.types.evaluation_report import EvaluationReport

from strands_agents.evals.evaluators.live_media_judge import (
    LiveMediaJudgeEvaluator,
    _JudgeResult,
)
from strands_agents.evals.experiments.media_corpus import (
    build_cases,
    build_media_corpus_experiment,
    media_task,
)
from strands_agents.evals.fixtures.manifest import load_manifest


# ---------------------------------------------------------------------------
# Offline tests — deterministic fake judge injected into the evaluator.
# ---------------------------------------------------------------------------


def _fake_video_judge_oracle(
    local_path: str,
    public_url: str | None,
    prompt: str,
) -> tuple[_JudgeResult, ...]:
    """Deterministic video judge that reads the answer from the filename.

    Fixture ids starting with ``video_`` followed by a
    ``hello``/``solid_red``/``moving_text`` verb map to yes; everything
    else maps to no. Mirrors the pinned expected_verdicts in the
    manifest exactly, so a correctly-wired Experiment scores 1.0
    across the board.

    When a ``public_url`` is supplied, emits both Gemini and Qwen
    results so the consensus branch is exercised.
    """
    name = os.path.basename(local_path)
    yes_names = {
        "video_hello_red.mp4",
        "video_solid_red.mp4",
        "video_moving_text.mp4",
    }
    answer_yes = name in yes_names
    gemini = _JudgeResult(
        model="gemini-2.5-flash",
        ran=True,
        judged_yes=answer_yes,
        raw_text="yes" if answer_yes else "no",
    )
    if public_url:
        qwen = _JudgeResult(
            model="qwen-vl-plus",
            ran=True,
            judged_yes=answer_yes,
            raw_text="yes" if answer_yes else "no",
        )
        return (gemini, qwen)
    return (gemini,)


def _fake_audio_judge_oracle(local_path: str, prompt: str) -> _JudgeResult:
    """Deterministic audio judge mirroring the manifest verdicts."""
    name = os.path.basename(local_path)
    yes_names = {
        "audio_hello_narration.wav",
        "audio_english_narration.wav",
    }
    answer_yes = name in yes_names
    return _JudgeResult(
        model="gemini-2.5-flash",
        ran=True,
        judged_yes=answer_yes,
        raw_text="yes" if answer_yes else "no",
    )


def _fake_video_judge_liar(
    local_path: str,
    public_url: str | None,
    prompt: str,
) -> tuple[_JudgeResult, ...]:
    """Judge that always answers yes. Used to prove the evaluator fails loudly."""
    return (
        _JudgeResult(
            model="gemini-2.5-flash",
            ran=True,
            judged_yes=True,
            raw_text="yes",
        ),
    )


def _fake_video_judge_abstaining(
    local_path: str,
    public_url: str | None,
    prompt: str,
) -> tuple[_JudgeResult, ...]:
    """Judge that refuses to answer. Used to prove skipped handling."""
    return (
        _JudgeResult(
            model="gemini-2.5-flash",
            ran=False,
            judged_yes=False,
            raw_text="",
            error="simulated safety filter",
        ),
    )


def test_offline_oracle_video_all_pass() -> None:
    """Oracle-judge video run: every case scores 1.0."""
    exp = build_media_corpus_experiment(media="video")
    exp.evaluators = [
        LiveMediaJudgeEvaluator(video_judge=_fake_video_judge_oracle)
    ]
    reports = exp.run_evaluations(task=media_task)
    assert reports, "Experiment must return at least one report"
    for report in reports:
        _assert_every_case_passes_strict(report)


def test_offline_oracle_audio_all_pass() -> None:
    """Oracle-judge audio run: every case scores 1.0."""
    exp = build_media_corpus_experiment(media="audio")
    exp.evaluators = [
        LiveMediaJudgeEvaluator(audio_judge=_fake_audio_judge_oracle)
    ]
    reports = exp.run_evaluations(task=media_task)
    assert reports, "Experiment must return at least one report"
    for report in reports:
        _assert_every_case_passes_strict(report)


def test_offline_liar_judge_fails_on_goodbye() -> None:
    """A judge that says yes to every video must fail on ``GOODBYE``.

    The GOODBYE fixture's expected_verdict is ``no`` — a liar judge
    answering yes flips the test red. This is the core invariant
    guarding against a judge regression.
    """
    exp = build_media_corpus_experiment(media="video")
    exp.evaluators = [
        LiveMediaJudgeEvaluator(video_judge=_fake_video_judge_liar)
    ]
    reports = exp.run_evaluations(task=media_task)
    failing_cases = _cases_with_any_hard_failure(reports)
    assert "fixture.video_goodbye_green" in failing_cases, (
        f"liar judge must fail on goodbye_green; failing={failing_cases}"
    )


def test_offline_abstaining_judge_reports_skipped_not_pass() -> None:
    """A judge that abstains emits a skipped label but does not silently pass.

    The Experiment still returns ``test_pass=True`` on the skipped
    output so the harness doesn't crash, but the label makes the
    skip visible to anyone reading the report — no silent grade
    inflation.
    """
    exp = build_media_corpus_experiment(media="video")
    exp.evaluators = [
        LiveMediaJudgeEvaluator(video_judge=_fake_video_judge_abstaining)
    ]
    reports = exp.run_evaluations(task=media_task)
    skipped_labels = [
        o.label
        for r in reports
        for per_case in r.detailed_results
        for o in per_case
        if (o.label or "").startswith("judge.skipped.")
    ]
    assert skipped_labels, "abstaining judge should surface skipped labels"


def test_offline_consensus_mode_emits_consensus_label() -> None:
    """When ``public_url`` is populated, a consensus label is emitted.

    None of the committed fixtures set ``public_url`` today, so we
    synthesize a case that does. This pins the consensus branch
    so a regression in the two-judge path is caught offline.
    """
    from strands_evals.case import Case

    synthetic = Case[dict[str, Any], str](
        name="synthetic.url_fixture",
        input={
            "id": "synthetic_with_url",
            "axis": "text_present_hello",
            "media": "video",
            "relative_path": "video/video_hello_red.mp4",
            "sha256": "ignored",
            "prompt": "Does this video show the word HELLO? Answer yes or no.",
            "expected_verdict": "yes",
            "public_url": "https://example.invalid/video_hello_red.mp4",
        },
        expected_output="yes",
    )

    from strands_evals.experiment import Experiment

    exp = Experiment[dict[str, Any], str](
        cases=[synthetic],
        evaluators=[
            LiveMediaJudgeEvaluator(video_judge=_fake_video_judge_oracle)
        ],
    )
    reports = exp.run_evaluations(task=media_task)
    consensus_labels = [
        o.label
        for r in reports
        for per_case in r.detailed_results
        for o in per_case
        if (o.label or "").startswith("judge.consensus.")
    ]
    assert consensus_labels, "expected a judge.consensus label when public_url is set"


def test_manifest_has_binary_fixtures_in_both_kinds() -> None:
    """Both video and audio have at least one yes and one no fixture.

    Guards against a regression where a refactor drops all ``no`` or
    all ``yes`` fixtures — the Experiment would score 100% on a
    trivially-biased judge in that case.
    """
    manifest = load_manifest()
    for media in ("video", "audio"):
        media_entries = [e for e in manifest.entries if e.media == media]
        yes = [e for e in media_entries if e.expected_verdict == "yes"]
        no = [e for e in media_entries if e.expected_verdict == "no"]
        assert yes and no, (
            f"{media} corpus missing a yes/no pair: "
            f"yes={len(yes)} no={len(no)}"
        )


def test_build_cases_filters_reject_verdicts_by_default() -> None:
    """By default ``reject`` fixtures do not enroll in a judge experiment."""
    video_cases = build_cases(media="video")
    for case in video_cases:
        assert (case.expected_output or "").lower() in {"yes", "no"}, (
            f"reject fixture leaked into judge experiment: {case.name}"
        )


# ---------------------------------------------------------------------------
# Live tests — skipped when keys are missing.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("GOOGLE_API_KEY"),
    reason="GOOGLE_API_KEY not set; live judge unavailable",
)
def test_live_video_corpus_matches_manifest_verdicts() -> None:
    """Run the video corpus against the real Gemini judge.

    Every committed fixture has a pinned verdict. The production
    Gemini model must agree with every one of them. A flip is a
    judge-regression signal, not a test bug.
    """
    exp = build_media_corpus_experiment(media="video")
    reports = exp.run_evaluations(task=media_task)
    for report in reports:
        _assert_every_case_passes_strict(report)


@pytest.mark.skipif(
    not os.environ.get("GOOGLE_API_KEY"),
    reason="GOOGLE_API_KEY not set; live judge unavailable",
)
def test_live_audio_corpus_matches_manifest_verdicts() -> None:
    """Run the audio corpus against the real Gemini judge."""
    exp = build_media_corpus_experiment(media="audio")
    reports = exp.run_evaluations(task=media_task)
    for report in reports:
        _assert_every_case_passes_strict(report)


# ---------------------------------------------------------------------------
# Small helpers — centralise report poking so tests stay readable.
# ---------------------------------------------------------------------------


def _case_name_of(report: EvaluationReport, index: int) -> str:
    """Return the case name at ``index`` within a report, tolerating shape.

    :class:`EvaluationReport.cases` is a list of dicts (serialized
    :class:`EvaluationData`); each entry has a ``name`` key when the
    :class:`Case` was constructed with one.
    """
    cases = report.cases or []
    if index >= len(cases):
        return f"case[{index}]"
    return str(cases[index].get("name") or f"case[{index}]")


def _cases_with_any_hard_failure(
    reports: list[EvaluationReport],
) -> set[str]:
    """Return the set of case names with at least one non-skipped failure.

    Used by the liar-judge test: we don't care about every case, just
    that the known-bad ones flip red.
    """
    failing: set[str] = set()
    for report in reports:
        for idx, per_case in enumerate(report.detailed_results):
            for out in per_case:
                if getattr(out, "test_pass", True):
                    continue
                if (out.label or "").startswith("judge.skipped."):
                    continue
                failing.add(_case_name_of(report, idx))
    return failing


def test_default_video_judge_honours_consensus_disabled_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a disabled voter must surface as ``ran=False``.

    ``VideoConsensus`` historically stored the error string in
    ``gemini_text`` / ``qwen_text`` when a voter was disabled, which
    made ``bool(gemini_text)`` look like a real answer and flipped
    valid "yes" fixtures to "no". The evaluator now reads the
    explicit ``*_disabled`` flags, and this test pins that behaviour:
    a stub ``judge_video_consensus`` that reports ``gemini_disabled=
    True`` must produce a ``_JudgeResult`` with ``ran=False`` and a
    populated ``error``, not a silent "no" answer.
    """
    from strands_agents.evals.evaluators.live_media_judge import (
        _default_video_judge,
    )
    from strands_agents.tests.live import _judges as judges_module

    class _FakeConsensus:
        gemini_yes = False
        qwen_yes = True
        agree = False
        gemini_text = "GOOGLE_API_KEY not set."
        qwen_text = "yes"
        gemini_disabled = True
        qwen_disabled = False

    def _fake_consensus(**_kwargs: Any) -> _FakeConsensus:
        return _FakeConsensus()

    monkeypatch.setattr(judges_module, "judge_video_consensus", _fake_consensus)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dummy")

    results = _default_video_judge(
        local_path="/tmp/fake.mp4",
        public_url="https://example.com/fake.mp4",
        prompt="is this video of a cat?",
    )

    assert len(results) == 2, "consensus path must return two JudgeResults"
    gemini, qwen = results
    assert gemini.model == "gemini-2.5-flash"
    assert gemini.ran is False, "disabled gemini must not be marked ran"
    assert gemini.error, "disabled gemini must surface an error string"
    assert qwen.model == "qwen-vl-plus"
    assert qwen.ran is True, "enabled qwen must be marked ran"
    assert qwen.judged_yes is True
    assert qwen.error is None


def _assert_every_case_passes_strict(report: EvaluationReport) -> None:
    """Strict assertion: every case in a report must pass every output.

    A ``judge.skipped`` label is tolerated (means a judge couldn't
    run for infra reasons) but a ``judge.<model>`` or
    ``judge.consensus`` failure means the model got it wrong — red
    test, real signal.
    """
    hard_failures: list[tuple[str, str, str]] = []
    for idx, per_case in enumerate(report.detailed_results):
        for out in per_case:
            if getattr(out, "test_pass", True):
                continue
            if (out.label or "").startswith("judge.skipped."):
                continue
            hard_failures.append(
                (_case_name_of(report, idx), out.label or "", out.reason or "")
            )
    assert not hard_failures, (
        f"evaluator={report.evaluator_name!r} had "
        f"{len(hard_failures)} hard failures: {hard_failures[:5]}"
    )
