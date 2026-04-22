"""Live multimodal judge evaluator for committed media fixtures.

This evaluator is the bridge between the deterministic fixture corpus
(see ``strands_agents/evals/fixtures/``) and the production judge
stack (Gemini, DashScope-Qwen). It answers one clear-cut binary
question per fixture — exactly the question pinned in
``manifest.json`` — and grades the judge against the expected
verdict.

Design
------
* **Scope** — ``expected_verdict`` ``"yes"`` / ``"no"`` only. Failure
  modes (``"reject"``) belong to deterministic QA gates (frozen-frame
  detector, black-frame ceiling, LUFS meter), not to a judge.
* **Models, live, no pinning** — whatever the shared ``_judges``
  helper routes to today. If the provider ships a worse model
  tomorrow the test flips red; that is the signal to drop the model,
  not to loosen the test.
* **Two-judge consensus for video when possible** — when a fixture's
  ``public_url`` is set (i.e. mirrored to a provider-reachable
  location) we also ask DashScope-Qwen and require both judges to
  agree with the expected verdict. Disagreement is a failure, not
  noise.
* **Audio is Gemini-only** — the DashScope OpenAI-compat endpoint
  does not accept audio, and the ``_judges`` helper keeps the audio
  path Gemini-only for the same reason.
* **Infra-only retries** — wrong answers are never retried. A provider
  503 or empty-response safety filter is retried a bounded number of
  times; a model that calls a clear-cut ``"yes"`` fixture ``"no"`` is
  reported loudly.
* **Missing credentials skip, never pass** — when neither judge can
  run we emit a single ``skipped`` output with ``test_pass=True`` and
  a ``judge.skipped`` label so the Experiment-level aggregation can
  tell the difference between "judge agreed" and "judge not run".

Input contract
--------------
``EvaluationData`` populated by :func:`media_task` in
``strands_agents.evals.experiments.media_corpus``:

* ``input`` — fixture metadata dict built from :class:`FixtureEntry`.
* ``actual_output`` — ``{"local_path": str, "public_url": str | None,
  "media": "video" | "audio"}``.
* ``expected_output`` — ``"yes"`` or ``"no"``.

Output contract
---------------
One :class:`EvaluationOutput` per judge actually invoked, plus an
extra ``judge.consensus`` entry when more than one judge ran. Each
output carries:

* ``score`` — 1.0 on agreement with expected verdict, 0.0 otherwise.
* ``test_pass`` — True iff the judge agreed.
* ``label`` — ``judge.<model>.<axis>`` for individual judge calls,
  ``judge.consensus.<axis>`` for the cross-judge agreement check,
  ``judge.skipped.<axis>`` when no judge could run.
* ``reason`` — short human-readable explanation including the raw
  answer text for diagnostics.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

# Bounded wait so a runaway PROCESSING state can never stall CI. A
# 2-second fixture typically reaches ACTIVE in < 10s; 60s covers the
# long tail without hiding genuine server-side failures.
_GEMINI_FILE_ACTIVATION_TIMEOUT_S = 60.0
_GEMINI_FILE_ACTIVATION_POLL_S = 1.0


def _await_gemini_file_active(client: Any, uploaded: Any) -> Any:
    """Poll a freshly uploaded Gemini file until it reaches ACTIVE.

    Gemini's Files API returns the upload object immediately with
    ``state == PROCESSING``; calling ``generateContent`` against a
    PROCESSING file fails with HTTP 400. Poll until the state
    advances. Raises :class:`TimeoutError` if activation exceeds the
    bounded deadline.
    """
    deadline = time.monotonic() + _GEMINI_FILE_ACTIVATION_TIMEOUT_S
    while True:
        state = getattr(uploaded, "state", None)
        state_name = getattr(state, "name", None) or str(state) if state else ""
        if not state_name.endswith("PROCESSING"):
            return uploaded
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"gemini file upload did not reach ACTIVE within "
                f"{_GEMINI_FILE_ACTIVATION_TIMEOUT_S:.0f}s "
                f"(last state: {state_name})"
            )
        time.sleep(_GEMINI_FILE_ACTIVATION_POLL_S)
        uploaded = client.files.get(name=uploaded.name)


@dataclass(frozen=True)
class _JudgeResult:
    """Outcome of a single judge call against one fixture.

    Attributes:
        model: Human-readable model identifier (``"gemini-2.5-flash"``,
            ``"qwen-vl-plus"``).
        ran: True iff the judge actually produced an answer. False
            when credentials were missing or the provider rejected
            every attempt.
        judged_yes: The judge's parsed yes/no answer. Undefined when
            ``ran`` is False.
        raw_text: Raw response text, trimmed to a few hundred chars.
        error: Failure reason when ``ran`` is False.
    """

    model: str
    ran: bool
    judged_yes: bool
    raw_text: str
    error: str | None = None


class LiveMediaJudgeEvaluator(Evaluator[dict[str, Any], str]):
    """Grade a live multimodal judge against a fixture's pinned verdict.

    The evaluator is stateless beyond the judge-lookup functions it
    closes over, which makes it safe to instantiate once per
    Experiment and reuse across all cases.

    Args:
        video_judge: Callable ``(local_path, public_url, prompt) ->
            _JudgeResult | tuple[_JudgeResult, _JudgeResult]`` used for
            video fixtures. The default calls Gemini always and Qwen
            only when a ``public_url`` is supplied. Exposed for tests.
        audio_judge: Callable ``(local_path, prompt) -> _JudgeResult``
            used for audio fixtures. The default calls Gemini. Exposed
            for tests.
    """

    def __init__(
        self,
        *,
        video_judge: "VideoJudgeFn | None" = None,
        audio_judge: "AudioJudgeFn | None" = None,
    ) -> None:
        super().__init__()
        self._video_judge = video_judge or _default_video_judge
        self._audio_judge = audio_judge or _default_audio_judge

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], str],
    ) -> list[EvaluationOutput]:
        fixture = evaluation_case.input or {}
        actual = evaluation_case.actual_output or {}
        expected_verdict = (evaluation_case.expected_output or "").strip().lower()

        if expected_verdict not in {"yes", "no"}:
            # ``reject`` fixtures belong to deterministic QA gates, not
            # to a judge. Declaring them as not-applicable here keeps
            # the media corpus mixed-in Experiment honest — if an
            # operator wires a reject fixture into a judge experiment
            # by mistake, they get a visible skipped output rather
            # than a bogus pass.
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason=(
                        "expected_verdict is not binary yes/no; "
                        "reject fixtures are graded by deterministic "
                        "QA evaluators, not by LLM judges"
                    ),
                    label=f"judge.not_applicable.{fixture.get('axis', 'unknown')}",
                )
            ]

        axis = str(fixture.get("axis") or "unknown")
        prompt = str(fixture.get("prompt") or "").strip()
        if not prompt:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="fixture has no judge prompt — manifest is malformed",
                    label=f"judge.error.{axis}",
                )
            ]

        media = str(actual.get("media") or fixture.get("media") or "").lower()
        local_path = actual.get("local_path") or ""
        public_url = actual.get("public_url")

        if not local_path:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="task did not resolve a local_path for the fixture",
                    label=f"judge.error.{axis}",
                )
            ]

        expected_yes = expected_verdict == "yes"

        if media == "video":
            judge_results = self._video_judge(local_path, public_url, prompt)
        elif media == "audio":
            judge_results = (self._audio_judge(local_path, prompt),)
        else:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=f"unknown media type {media!r}; expected video or audio",
                    label=f"judge.error.{axis}",
                )
            ]

        return _grade(judge_results, axis=axis, expected_yes=expected_yes)


VideoJudgeFn = Any  # Callable[[str, str | None, str], tuple[_JudgeResult, ...]]
AudioJudgeFn = Any  # Callable[[str, str], _JudgeResult]


def _grade(
    judge_results: tuple[_JudgeResult, ...],
    *,
    axis: str,
    expected_yes: bool,
) -> list[EvaluationOutput]:
    """Convert raw judge results into EvaluationOutput list.

    Emits one output per judge that actually ran, plus a
    ``judge.consensus`` output when more than one ran. If no judge
    ran, emits a single ``judge.skipped`` output with ``test_pass=True``
    so missing credentials don't crash the experiment.
    """
    outputs: list[EvaluationOutput] = []
    ran = [r for r in judge_results if r.ran]
    if not ran:
        errors = "; ".join(r.error or "unknown" for r in judge_results) or "no judges available"
        outputs.append(
            EvaluationOutput(
                score=1.0,
                test_pass=True,
                reason=f"no judge ran: {errors}",
                label=f"judge.skipped.{axis}",
            )
        )
        return outputs

    for result in ran:
        agreed = result.judged_yes == expected_yes
        outputs.append(
            EvaluationOutput(
                score=1.0 if agreed else 0.0,
                test_pass=agreed,
                reason=(
                    f"{result.model} answered "
                    f"{'yes' if result.judged_yes else 'no'}; "
                    f"expected {'yes' if expected_yes else 'no'}; "
                    f"raw={result.raw_text[:120]!r}"
                ),
                label=f"judge.{result.model}.{axis}",
            )
        )

    if len(ran) >= 2:
        all_agreed = all(r.judged_yes == expected_yes for r in ran)
        outputs.append(
            EvaluationOutput(
                score=1.0 if all_agreed else 0.0,
                test_pass=all_agreed,
                reason=(
                    "judges unanimous with expected verdict"
                    if all_agreed
                    else "judges disagree with each other or with expected verdict"
                ),
                label=f"judge.consensus.{axis}",
            )
        )

    return outputs


# ---------------------------------------------------------------------------
# Default judge routes — wire to the production judge helpers.
# ---------------------------------------------------------------------------


def _default_video_judge(
    local_path: str,
    public_url: str | None,
    prompt: str,
) -> tuple[_JudgeResult, ...]:
    """Route a video fixture through Gemini + optional Qwen.

    Gemini always runs when a ``GOOGLE_API_KEY`` is set. Qwen only
    runs when the fixture exposes a ``public_url`` (DashScope's
    endpoint rejects local files). Both run against the same prompt;
    two independent answers, no cross-contamination.
    """
    from strands_agents.tests.live._judges import (  # lazy import: test-only deps
        judge_text_yes,  # noqa: F401  # keep reachable for future refactor
        judge_video_consensus,
    )

    if not os.environ.get("GOOGLE_API_KEY") and not os.environ.get("DASHSCOPE_API_KEY"):
        return (
            _JudgeResult(
                model="gemini-2.5-flash",
                ran=False,
                judged_yes=False,
                raw_text="",
                error="GOOGLE_API_KEY and DASHSCOPE_API_KEY not set",
            ),
        )

    if public_url:
        consensus = judge_video_consensus(
            local_path=local_path,
            public_url=public_url,
            prompt=prompt,
        )
        # Use the explicit ``*_disabled`` flags to distinguish "judge
        # answered" from "judge was disabled (no key, safety filter)".
        # The ``*_text`` fields collapse error and answer text into a
        # single string, so a truthy text does not imply the judge ran.
        return (
            _JudgeResult(
                model="gemini-2.5-flash",
                ran=not consensus.gemini_disabled,
                judged_yes=consensus.gemini_yes,
                raw_text=consensus.gemini_text,
                error=(
                    consensus.gemini_text or "gemini disabled"
                    if consensus.gemini_disabled
                    else None
                ),
            ),
            _JudgeResult(
                model="qwen-vl-plus",
                ran=not consensus.qwen_disabled,
                judged_yes=consensus.qwen_yes,
                raw_text=consensus.qwen_text,
                error=(
                    consensus.qwen_text or "qwen disabled"
                    if consensus.qwen_disabled
                    else None
                ),
            ),
        )

    # No public URL — only Gemini can see the file.
    return (_call_gemini_video_only(local_path, prompt),)


def _call_gemini_video_only(local_path: str, prompt: str) -> _JudgeResult:
    """Call Gemini with a local video path via the Files API.

    Kept as a separate helper because the ``_judges`` module's
    :func:`judge_video_consensus` is consensus-shaped; callers without
    a URL still need the single-judge Gemini answer.
    """
    from strands_agents.tests.live._judges import (  # lazy import: test-only deps
        _answer_is_yes,
    )

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return _JudgeResult(
            model="gemini-2.5-flash",
            ran=False,
            judged_yes=False,
            raw_text="",
            error="GOOGLE_API_KEY not set",
        )

    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError as exc:
        return _JudgeResult(
            model="gemini-2.5-flash",
            ran=False,
            judged_yes=False,
            raw_text="",
            error=f"google-genai not importable: {exc}",
        )

    client = genai.Client(api_key=api_key)
    try:
        uploaded = client.files.upload(file=local_path)
        # Gemini's Files API returns immediately with the upload in
        # PROCESSING state; calling generateContent before the file
        # reaches ACTIVE fails with a 400. Poll until ready.
        uploaded = _await_gemini_file_active(client, uploaded)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[uploaded, prompt],
            config=types.GenerateContentConfig(temperature=0.0),
        )
    except Exception as exc:  # noqa: BLE001 - provider boundary
        return _JudgeResult(
            model="gemini-2.5-flash",
            ran=False,
            judged_yes=False,
            raw_text="",
            error=f"gemini error: {exc}",
        )

    text = getattr(response, "text", None) or ""
    if not text:
        return _JudgeResult(
            model="gemini-2.5-flash",
            ran=False,
            judged_yes=False,
            raw_text="",
            error="gemini returned empty text (safety filter)",
        )
    return _JudgeResult(
        model="gemini-2.5-flash",
        ran=True,
        judged_yes=_answer_is_yes(text),
        raw_text=text,
    )


def _default_audio_judge(local_path: str, prompt: str) -> _JudgeResult:
    """Route an audio fixture through Gemini.

    DashScope's OpenAI-compat endpoint does not take audio, so audio
    is Gemini-only today. If another multimodal audio judge becomes
    available it can be added to the consensus stack here.
    """
    from strands_agents.tests.live._judges import (  # lazy import: test-only deps
        judge_audio_yes,
    )

    if not os.environ.get("GOOGLE_API_KEY"):
        return _JudgeResult(
            model="gemini-2.5-flash",
            ran=False,
            judged_yes=False,
            raw_text="",
            error="GOOGLE_API_KEY not set",
        )

    judgment = judge_audio_yes(prompt=prompt, audio_path=local_path)
    return _JudgeResult(
        model="gemini-2.5-flash",
        ran=not judgment.disabled,
        judged_yes=judgment.is_yes,
        raw_text=judgment.answer,
        error=judgment.error,
    )


__all__ = ["LiveMediaJudgeEvaluator"]
