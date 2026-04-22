"""Shared live-judge helpers for the per-component proof-of-robustness suite.

This module wraps production-grade cloud-API LLMs (Google Gemini, Alibaba
DashScope Qwen, Anthropic Claude) behind a few small helpers so the
per-component tests under ``tests/live/components/`` don't each reinvent
the same boilerplate.

Judge policy (per user directives, captured in AGENTS.md):

* Pass/fail gate contains only clear-cut questions.  If a judge flips
  answers on the same prompt, the judge is broken — not the test.
* Production models, live, no version pinning.  We hit whatever the
  provider ships today; a failing test signals the model is a discard
  candidate.
* Temperature 0 everywhere the provider exposes it.  Determinism matters.
* Infra hiccups (HTTP 503, socket timeout, rate limit) are retried a
  small, bounded number of times.  A *judgment* failure — a wrong answer
  to a clear-cut question — is never retried.  That's the signal we're
  testing for.
* Safety-critical calls require two-judge consensus.  Gemini and Qwen
  must both flag "this is clearly bad."  Disagreement fails the test
  rather than silently picking one judge's opinion.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from tools.qa_jury import (
    DashscopeQwenVoter,
    GeminiVoter,
    VideoClip,
    VoterVerdict,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Retry + determinism policy (shared across all live component tests).
# ---------------------------------------------------------------------------

INFRA_RETRY_ATTEMPTS = 3
"""Maximum number of attempts against a provider for one logical call.

Three is enough to ride out the typical 503-under-load window without
hiding an actual model regression.  More than this starts masking real
failures.
"""

INFRA_RETRY_SLEEP_S = 3.0
"""Fixed delay between infra retries, in seconds."""

DETERMINISM_RUNS = 3
"""Number of identical runs required to prove a judge is deterministic."""


# ---------------------------------------------------------------------------
# Text-binary judge (Gemini Flash, temperature 0).
# ---------------------------------------------------------------------------


def _ensure_dashscope_intl_key() -> None:
    """Map ``DASHSCOPE_API_KEY`` onto ``DASHSCOPE_INTL_API_KEY`` if needed.

    Our ``DashscopeQwenVoter`` reads ``DASHSCOPE_INTL_API_KEY`` because the
    Alibaba intl endpoint uses a distinct token namespace; tests export
    whichever the environment happens to have.  This helper bridges the
    gap so the test body doesn't need to branch on it.
    """
    if os.environ.get("DASHSCOPE_INTL_API_KEY"):
        return
    legacy = os.environ.get("DASHSCOPE_API_KEY")
    if legacy:
        os.environ["DASHSCOPE_INTL_API_KEY"] = legacy


@dataclass(frozen=True)
class TextJudgment:
    """Outcome of a single text-only judge call.

    Attributes:
        answer: The raw text the judge returned.
        is_yes: True iff ``answer`` clearly starts with "yes" per
            :func:`_answer_is_yes`.
        model: The model identifier that produced the answer.
        disabled: True when the provider refused to answer (safety
            filter, empty response, rate-limit exhaustion).  Treat as an
            infra failure, not as a "no" verdict.
        error: Free-text error from the provider when ``disabled``.
    """

    answer: str
    is_yes: bool
    model: str
    disabled: bool = False
    error: str | None = None


def _answer_is_yes(text: str) -> bool:
    """Return True iff ``text`` reads as an affirmative single-word answer.

    The judge prompts in this suite always ask for "yes" or "no" as the
    first token.  A long-form answer that hedges, or one that starts
    with "no", fails.  Trailing punctuation is tolerated.
    """
    if not text:
        return False
    head = text.strip().lower().split(maxsplit=1)
    if not head:
        return False
    token = head[0].rstrip(",.!?\"'`:;")
    return token == "yes"


def _gemini_text_sync(model_name: str, prompt: str) -> TextJudgment:
    """Synchronous single-shot text-only call to Gemini at temperature 0.

    Args:
        model_name: The Gemini model identifier to call.
        prompt: The judge prompt (binary yes/no question).

    Returns:
        A :class:`TextJudgment` wrapping the model's answer or an
        ``disabled=True`` marker if the provider rejected the call.
    """
    from google import genai  # type: ignore
    from google.genai import types  # type: ignore

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return TextJudgment(
            answer="",
            is_yes=False,
            model=model_name,
            disabled=True,
            error="GOOGLE_API_KEY not set.",
        )
    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[prompt],
            config=types.GenerateContentConfig(temperature=0.0),
        )
    except Exception as exc:  # noqa: BLE001 - provider adapter boundary
        logger.warning("model=<%s> | gemini text judge failed: %s", model_name, exc)
        return TextJudgment(
            answer="",
            is_yes=False,
            model=model_name,
            disabled=True,
            error=str(exc),
        )
    text = getattr(response, "text", None) or ""
    if not text:
        return TextJudgment(
            answer="",
            is_yes=False,
            model=model_name,
            disabled=True,
            error="Gemini returned an empty response (likely safety-filtered).",
        )
    return TextJudgment(
        answer=text,
        is_yes=_answer_is_yes(text),
        model=model_name,
    )


def judge_text_yes(
    prompt: str,
    *,
    model_name: str = "gemini-2.5-flash",
    attempts: int = INFRA_RETRY_ATTEMPTS,
    sleep_s: float = INFRA_RETRY_SLEEP_S,
) -> TextJudgment:
    """Binary text judge: return a :class:`TextJudgment` for ``prompt``.

    Retries only fire when the provider returns ``disabled`` (i.e. an
    infra-level failure).  An incorrect verdict (model says "no" when
    we expected "yes") is not retried — that is the signal the test
    wants to surface.

    Args:
        prompt: A yes/no question aimed at the judge.  Should end with
            "Answer with a single word: yes or no." to anchor parsing.
        model_name: Gemini model id.  Defaults to Flash for cost; pass
            ``"gemini-3-pro-preview"`` when reasoning quality matters.
        attempts: Maximum number of provider calls, including the first.
        sleep_s: Delay between attempts, in seconds.

    Returns:
        The first non-disabled :class:`TextJudgment` encountered, or the
        final disabled one if every attempt failed.
    """
    last = _gemini_text_sync(model_name, prompt)
    for _ in range(1, attempts):
        if not last.disabled:
            return last
        import time

        time.sleep(sleep_s)
        last = _gemini_text_sync(model_name, prompt)
    return last


def judge_text_deterministic_yes(
    prompt: str,
    *,
    model_name: str = "gemini-2.5-flash",
    runs: int = DETERMINISM_RUNS,
) -> list[TextJudgment]:
    """Run :func:`judge_text_yes` ``runs`` times with identical input.

    Used to prove a judge gives the same answer to a clear-cut question
    every time.  A judge that flips answers is a judge we cannot use as
    a PR gate.

    Args:
        prompt: The judge prompt.
        model_name: Gemini model id.
        runs: Number of identical calls (default 3).

    Returns:
        The list of judgments, one per run.  The test asserts all
        ``is_yes`` flags agree.
    """
    return [
        judge_text_yes(prompt, model_name=model_name) for _ in range(runs)
    ]


# ---------------------------------------------------------------------------
# Video-binary consensus (Gemini + Qwen).  Inherited from the PR-G.1 gate.
# ---------------------------------------------------------------------------


async def _call_video_with_retries(
    voter, clip: VideoClip, prompt: str,
    *,
    attempts: int = INFRA_RETRY_ATTEMPTS,
    sleep_s: float = INFRA_RETRY_SLEEP_S,
) -> VoterVerdict:
    """Call a qa_jury ``Voter`` with infra-only retries.

    Mirrors the retry shape used by the ``test_production_judges`` gate
    tests so video judgments behave the same way across the whole live
    suite.
    """
    last = await voter.judge(clip, prompt)
    for _ in range(1, attempts):
        if not last.disabled:
            return last
        await asyncio.sleep(sleep_s)
        last = await voter.judge(clip, prompt)
    return last


@dataclass(frozen=True)
class VideoConsensus:
    """Result of a two-judge video consensus call.

    Attributes:
        gemini_yes: True iff Gemini's answer parses as "yes".
        qwen_yes: True iff Qwen's answer parses as "yes".
        agree: True iff both flags match.  Disagreement fails the test.
        gemini_text: Raw Gemini answer text, or error/diagnostic text when
            the judge was disabled or returned nothing usable.  Callers that
            need to distinguish "answered" from "disabled" must consult the
            ``gemini_disabled`` / ``qwen_disabled`` flags instead of parsing
            this string.
        qwen_text: Raw Qwen answer, same semantics as ``gemini_text``.
        gemini_disabled: True iff the Gemini voter was disabled (missing
            credentials, provider error, safety filter) and did not return a
            real answer.
        qwen_disabled: True iff the Qwen voter was disabled for the same
            reasons.
    """

    gemini_yes: bool
    qwen_yes: bool
    agree: bool
    gemini_text: str
    qwen_text: str
    gemini_disabled: bool = False
    qwen_disabled: bool = False


def judge_video_consensus(
    local_path: str,
    public_url: str,
    prompt: str,
    *,
    visual_phrase: str = "",
    duration_s: float = 0.0,
    gemini_model: str = "gemini-2.5-flash",
) -> VideoConsensus:
    """Two-judge (Gemini + Qwen) consensus on a binary video question.

    The Gemini voter takes the local file via the Files API; the Qwen
    voter over DashScope's OpenAI-compat endpoint only accepts a public
    URL.  Both pre-conditions have to be satisfied for this call to
    proceed — provide both.

    Args:
        local_path: Absolute path to the video on disk, for Gemini.
        public_url: Public URL of the same video, for Qwen.
        prompt: The yes/no question for both judges.
        visual_phrase: Optional descriptive phrase for the artifact.
        duration_s: Optional clip duration, purely informational.
        gemini_model: Gemini model id.  Defaults to Flash for cost.

    Returns:
        A :class:`VideoConsensus` with the raw answers, parsed flags,
        and the ``agree`` flag.  Callers assert both judgments match
        their expected outcome.
    """
    _ensure_dashscope_intl_key()
    clip = VideoClip(
        artifact_id="live_judge",
        path=local_path,
        url=public_url,
        visual_phrase=visual_phrase,
        duration_s=duration_s,
    )
    gemini = GeminiVoter(model_name=gemini_model)
    qwen = DashscopeQwenVoter()

    gemini_verdict = asyncio.run(_call_video_with_retries(gemini, clip, prompt))
    qwen_verdict = asyncio.run(_call_video_with_retries(qwen, clip, prompt))

    gemini_text = str(gemini_verdict.value or "")
    qwen_text = str(qwen_verdict.value or "")
    gemini_yes = _answer_is_yes(gemini_text) and not gemini_verdict.disabled
    qwen_yes = _answer_is_yes(qwen_text) and not qwen_verdict.disabled
    return VideoConsensus(
        gemini_yes=gemini_yes,
        qwen_yes=qwen_yes,
        agree=(gemini_yes == qwen_yes),
        gemini_text=gemini_text or (gemini_verdict.error or ""),
        qwen_text=qwen_text or (qwen_verdict.error or ""),
        gemini_disabled=gemini_verdict.disabled,
        qwen_disabled=qwen_verdict.disabled,
    )


# ---------------------------------------------------------------------------
# Audio-binary judge (Gemini Flash, multimodal, temperature 0).
# ---------------------------------------------------------------------------


def _gemini_audio_sync(
    model_name: str, prompt: str, audio_path: str, mime_type: str
) -> TextJudgment:
    """Synchronous single-shot audio+text call to Gemini at temperature 0.

    Args:
        model_name: The Gemini model identifier to call.
        prompt: The judge prompt (binary yes/no question).
        audio_path: Absolute path to the audio file to attach.
        mime_type: MIME type of the audio (``audio/wav``, ``audio/mpeg``).

    Returns:
        A :class:`TextJudgment` wrapping the model's answer or an
        ``disabled=True`` marker if the provider rejected the call.
    """
    from google import genai  # type: ignore
    from google.genai import types  # type: ignore

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return TextJudgment(
            answer="",
            is_yes=False,
            model=model_name,
            disabled=True,
            error="GOOGLE_API_KEY not set.",
        )
    client = genai.Client(api_key=api_key)
    try:
        with open(audio_path, "rb") as handle:
            audio_bytes = handle.read()
        response = client.models.generate_content(
            model=model_name,
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                prompt,
            ],
            config=types.GenerateContentConfig(temperature=0.0),
        )
    except Exception as exc:  # noqa: BLE001 - provider adapter boundary
        logger.warning(
            "model=<%s>, path=<%s> | gemini audio judge failed: %s",
            model_name,
            audio_path,
            exc,
        )
        return TextJudgment(
            answer="",
            is_yes=False,
            model=model_name,
            disabled=True,
            error=str(exc),
        )
    text = getattr(response, "text", None) or ""
    if not text:
        return TextJudgment(
            answer="",
            is_yes=False,
            model=model_name,
            disabled=True,
            error="Gemini returned an empty response (likely safety-filtered).",
        )
    return TextJudgment(
        answer=text,
        is_yes=_answer_is_yes(text),
        model=model_name,
    )


def judge_audio_yes(
    prompt: str,
    audio_path: str,
    *,
    mime_type: str = "audio/wav",
    model_name: str = "gemini-2.5-flash",
    attempts: int = INFRA_RETRY_ATTEMPTS,
    sleep_s: float = INFRA_RETRY_SLEEP_S,
) -> TextJudgment:
    """Binary audio judge: does the clip at ``audio_path`` satisfy ``prompt``?

    Infra retries on a ``disabled`` result; never retries a wrong verdict.

    Args:
        prompt: A yes/no question about the audio.  Should end with
            "Answer with a single word: yes or no." to anchor parsing.
        audio_path: Absolute path to the audio file on disk.
        mime_type: MIME type; defaults to ``audio/wav``.
        model_name: Gemini model id.  Defaults to Flash for cost.
        attempts: Maximum number of provider calls, including the first.
        sleep_s: Delay between attempts, in seconds.

    Returns:
        The first non-disabled :class:`TextJudgment` encountered, or the
        final disabled one if every attempt failed.
    """
    last = _gemini_audio_sync(model_name, prompt, audio_path, mime_type)
    for _ in range(1, attempts):
        if not last.disabled:
            return last
        import time

        time.sleep(sleep_s)
        last = _gemini_audio_sync(model_name, prompt, audio_path, mime_type)
    return last


# ---------------------------------------------------------------------------
# Live text generator (Claude) for LLM-backed component helpers.
# ---------------------------------------------------------------------------


def live_claude_text(
    prompt: str,
    *,
    model: str = "claude-sonnet-4-5",
    max_tokens: int = 2048,
    system: str | None = None,
) -> str:
    """Call Anthropic Claude for a single text completion at temperature 0.

    Used by component tests that need an LLM-backed helper (scenario
    generator, content analyst, visual concepter) to produce a real
    artifact the test can then judge.  Claude is chosen over Gemini for
    generation because the Gemini generation path is what the *judge*
    uses — we don't want the generator and the judge to be the same
    model family.

    Args:
        prompt: The user prompt, as a single string.
        model: Anthropic model id.  Defaults to the latest Sonnet; upgrade
            as newer versions ship.  No pinning by policy.
        max_tokens: Output cap; 2k is plenty for a single scene/phrase.
        system: Optional system prompt.

    Returns:
        The model's response text, stripped.

    Raises:
        RuntimeError: If ``ANTHROPIC_API_KEY`` is not set, or the API
            returns an empty / blocked response.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    import anthropic  # type: ignore

    client = anthropic.Anthropic(api_key=api_key)
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    parts = getattr(response, "content", []) or []
    texts: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if text:
            texts.append(text)
    body = "\n".join(texts).strip()
    if not body:
        raise RuntimeError(
            f"Claude returned an empty response for prompt={prompt[:80]!r}"
        )
    return body
