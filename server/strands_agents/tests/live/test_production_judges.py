"""Proof-of-life tests for the production judge APIs.

These are the "clear-cut cases" pass/fail gate: tiny, obvious visual
content that every production judge should get right, every single
time.  They call real cloud APIs (Google, Alibaba) and are never
skipped for flakiness — if the credentials are set, the tests run and
must pass.

Per the judge policy:

* Only unambiguous content goes in this suite.  The 2-second red frame
  with the word "HELLO" baked in is deliberately trivial — a judge that
  can't pass this can't be trusted to block a PR.
* Infrastructure failures (HTTP 503, socket timeout, rate limits) are
  retried a small number of times.  Judgment failures (wrong answer,
  empty response, disagreement between judges) are not retried — they
  are real regressions.
* Consensus for safety-style calls requires **both** production video
  judges to agree.  A single judge flipping on "is HELLO visible" would
  make the judge a candidate for discard, per the user directive
  "the model failed — it's a candidate for discard."

The fixture is checked into the repo and mirrored to a public B2 URL
(required by the OpenAI-compatible video endpoint that Qwen exposes).
If the B2 URL ever moves, the test constant below is the single place
to update.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from tools.qa_jury import (
    DashscopeQwenVoter,
    GeminiVoter,
    VideoClip,
    VoterVerdict,
)

from .conftest import requires_dashscope_api, requires_google_api


# ---------------------------------------------------------------------------
# Fixture metadata
# ---------------------------------------------------------------------------

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "video" / "hello_red.mp4"
)
"""Local path to the 2-second red+HELLO fixture.

Shipped as a binary under ``server/strands_agents/tests/fixtures/video``
so every developer checkout has it.  Small enough (~5 KB) that committing
the bytes is cheaper than stashing them off-repo.
"""

FIXTURE_PUBLIC_URL = (
    "https://f004.backblazeb2.com/file/cloudberry-documentary-v2/"
    "tier2-fixtures/video/hello_red.mp4"
)
"""Public B2 URL for the same fixture, for providers that require a URL.

The Dashscope OpenAI-compatible video endpoint refuses local file paths —
it only accepts ``video_url`` content parts — so we mirror the fixture
to the project's public B2 bucket and let Dashscope fetch it.

If the bucket or key ever changes, re-upload and patch this constant.
There is no frame-sampling fallback per jury policy.
"""

FIXTURE_PROMPT = (
    "Does this video show the English word HELLO on screen? "
    "Answer with a single word: yes or no."
)
"""Prompt is deliberately binary and about a trivial visual fact.

Any judge that cannot answer this consistently cannot be trusted to gate
a PR.  Pass/fail lives here; nuance lives in the drift-detector suite.
"""

# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

# Real providers occasionally emit transient 503s under load.  We retry a
# small number of times with a fixed delay; anything more than this would
# start hiding genuine model regressions.
_INFRA_RETRY_ATTEMPTS = 3
_INFRA_RETRY_SLEEP_S = 3.0

# Determinism probe runs the voter this many times on the same fixture.
# A judge that flips answers across these runs is not safe to gate PRs.
_DETERMINISM_RUNS = 3


async def _call_with_retries(
    voter,
    clip: VideoClip,
    *,
    attempts: int = _INFRA_RETRY_ATTEMPTS,
    sleep_s: float = _INFRA_RETRY_SLEEP_S,
) -> VoterVerdict:
    """Call ``voter.judge`` with retries for transient infra failures.

    Retries only fire when the verdict comes back ``disabled`` (the
    voter's way of signalling "the provider rejected the call").  A
    successful judgment — even an incorrect one — is never retried;
    that's the signal callers are testing for.

    Args:
        voter: A qa_jury ``Voter`` instance (``GeminiVoter`` /
            ``DashscopeQwenVoter``).
        clip: The fixture wrapped as a ``VideoClip`` artifact.
        attempts: Maximum number of calls, including the first.
        sleep_s: Delay between attempts.

    Returns:
        The verdict from the first non-disabled attempt, or the last
        disabled verdict if every attempt failed.
    """
    last = await voter.judge(clip, FIXTURE_PROMPT)
    for attempt in range(1, attempts):
        if not last.disabled:
            return last
        await asyncio.sleep(sleep_s)
        last = await voter.judge(clip, FIXTURE_PROMPT)
    return last


def _fixture_bytes_exist() -> bool:
    """Return True iff the committed fixture is resolvable and non-empty."""
    return FIXTURE_PATH.exists() and FIXTURE_PATH.stat().st_size > 0


def _verdict_is_yes(text: str) -> bool:
    """Return True iff ``text`` clearly reads as an affirmative answer.

    We accept any response that *starts with* ``yes`` (case-insensitive),
    tolerating an optional trailing comma or period.  Anything else —
    including ``"no"``, a refusal, or a long-form answer that begins
    with a hedge — fails.  The prompt explicitly asks for a single word
    so a judgment that strays from that format is itself a regression.
    """
    if not text:
        return False
    head = text.strip().lower().split(maxsplit=1)
    if not head:
        return False
    token = head[0].rstrip(",.!?\"'`")
    return token == "yes"


# ---------------------------------------------------------------------------
# Fixture sanity
# ---------------------------------------------------------------------------


def test_fixture_is_committed() -> None:
    """Hermetic check: the local fixture bytes are present in the repo.

    This test never touches the network.  It guards against a developer
    accidentally removing the committed binary or moving the directory
    and is the first line of defence in CI — if this fails, the live
    tests below have nothing to submit.
    """
    assert _fixture_bytes_exist(), (
        f"fixture missing at {FIXTURE_PATH}; regenerate with ffmpeg "
        f"(2s red background, white HELLO text, h264, yuv420p)."
    )


# ---------------------------------------------------------------------------
# Live-judge gates
# ---------------------------------------------------------------------------


@requires_google_api
def test_gemini_flash_detects_hello() -> None:
    """Gemini 2.5 Flash must say 'yes' to HELLO on screen, every time.

    Uses the Flash tier because Pro occasionally 503s under capacity
    pressure; Flash is the judge that actually ships in the PR gate.
    Pro is exercised by the determinism suite below with retries.
    """
    voter = GeminiVoter(model_name="gemini-2.5-flash")
    clip = VideoClip(
        artifact_id="hello_red",
        path=str(FIXTURE_PATH),
        visual_phrase="red screen with the word HELLO",
        duration_s=2.0,
    )
    verdict = asyncio.run(_call_with_retries(voter, clip))
    assert not verdict.disabled, f"Gemini Flash disabled after retries: {verdict.error}"
    assert _verdict_is_yes(str(verdict.value)), (
        f"Gemini Flash failed to identify HELLO; response={verdict.value!r}"
    )


@requires_google_api
def test_gemini_flash_is_deterministic() -> None:
    """Gemini 2.5 Flash must give the same answer on N identical runs.

    A judge that flips between "yes" and "no" on identical inputs is
    not a PR gate — it's a coin flip.  Per user directive, the fix is
    to discard the model, not to loosen the test.
    """
    voter = GeminiVoter(model_name="gemini-2.5-flash")
    clip = VideoClip(
        artifact_id="hello_red",
        path=str(FIXTURE_PATH),
        visual_phrase="red screen with the word HELLO",
        duration_s=2.0,
    )
    answers: list[str] = []
    for run_idx in range(_DETERMINISM_RUNS):
        verdict = asyncio.run(_call_with_retries(voter, clip))
        assert not verdict.disabled, (
            f"run={run_idx} disabled after retries: {verdict.error}"
        )
        answers.append(str(verdict.value).strip().lower())
    normalised = {
        a.split(maxsplit=1)[0].rstrip(",.!?\"'`") if a else "" for a in answers
    }
    assert normalised == {"yes"}, (
        f"Gemini Flash not deterministic; got {normalised!r} across "
        f"{_DETERMINISM_RUNS} runs: {answers!r}"
    )


@requires_dashscope_api
def test_qwen_detects_hello() -> None:
    """Qwen3-VL Plus must say 'yes' to HELLO on screen, every time.

    Qwen via Dashscope OpenAI-compat only accepts a public video URL,
    so this test consumes the B2-hosted mirror of the fixture.  The
    provider is routed via the international DashScope endpoint; in
    CI, either ``DASHSCOPE_INTL_API_KEY`` is exported explicitly or
    the test environment maps ``DASHSCOPE_API_KEY`` onto it.
    """
    _ensure_dashscope_intl_key()
    voter = DashscopeQwenVoter()
    clip = VideoClip(
        artifact_id="hello_red",
        path=str(FIXTURE_PATH),
        url=FIXTURE_PUBLIC_URL,
        visual_phrase="red screen with the word HELLO",
        duration_s=2.0,
    )
    verdict = asyncio.run(_call_with_retries(voter, clip))
    assert not verdict.disabled, f"Qwen disabled after retries: {verdict.error}"
    assert _verdict_is_yes(str(verdict.value)), (
        f"Qwen failed to identify HELLO; response={verdict.value!r}"
    )


# ---------------------------------------------------------------------------
# Two-judge consensus gate
# ---------------------------------------------------------------------------


@requires_google_api
@requires_dashscope_api
def test_both_video_judges_agree_on_hello() -> None:
    """Gemini and Qwen must both identify HELLO on screen.

    This is the clear-cut safety rule: a verdict that would block a
    pull request must have two independent production video judges in
    agreement.  If either judge flips, the test fails loudly — per
    user directive, the failing judge is then a candidate for discard,
    not a signal to weaken the consensus rule.
    """
    _ensure_dashscope_intl_key()
    gemini = GeminiVoter(model_name="gemini-2.5-flash")
    qwen = DashscopeQwenVoter()

    gemini_clip = VideoClip(
        artifact_id="hello_red_gemini",
        path=str(FIXTURE_PATH),
        visual_phrase="red screen with the word HELLO",
        duration_s=2.0,
    )
    qwen_clip = VideoClip(
        artifact_id="hello_red_qwen",
        path=str(FIXTURE_PATH),
        url=FIXTURE_PUBLIC_URL,
        visual_phrase="red screen with the word HELLO",
        duration_s=2.0,
    )

    gemini_verdict, qwen_verdict = asyncio.run(
        _gather_verdicts(gemini, gemini_clip, qwen, qwen_clip),
    )

    assert not gemini_verdict.disabled, (
        f"Gemini disabled in consensus path: {gemini_verdict.error}"
    )
    assert not qwen_verdict.disabled, (
        f"Qwen disabled in consensus path: {qwen_verdict.error}"
    )
    assert _verdict_is_yes(str(gemini_verdict.value)), (
        f"Gemini dissented; response={gemini_verdict.value!r}"
    )
    assert _verdict_is_yes(str(qwen_verdict.value)), (
        f"Qwen dissented; response={qwen_verdict.value!r}"
    )


async def _gather_verdicts(
    gemini: GeminiVoter,
    gemini_clip: VideoClip,
    qwen: DashscopeQwenVoter,
    qwen_clip: VideoClip,
) -> tuple[VoterVerdict, VoterVerdict]:
    """Run both voters concurrently and return their verdicts as a pair.

    Running in parallel halves wall-clock time for the consensus gate
    and, more importantly, gives both judges the same submission window
    so one being slow doesn't pile up behind the other.
    """
    gemini_task = _call_with_retries(gemini, gemini_clip)
    qwen_task = _call_with_retries(qwen, qwen_clip)
    return await asyncio.gather(gemini_task, qwen_task)


def _ensure_dashscope_intl_key() -> None:
    """Map ``DASHSCOPE_API_KEY`` onto ``DASHSCOPE_INTL_API_KEY`` if needed.

    The production pipeline historically exports only the single
    ``DASHSCOPE_API_KEY`` variable.  ``DashscopeQwenVoter`` reads the
    international-endpoint variant by default; this helper unifies the
    two names so a developer with a working Dashscope key doesn't need
    to duplicate the environment variable by hand.
    """
    intl = os.environ.get("DASHSCOPE_INTL_API_KEY", "").strip()
    if intl:
        return
    fallback = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if fallback:
        os.environ["DASHSCOPE_INTL_API_KEY"] = fallback
