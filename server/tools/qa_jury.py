"""
QA Jury -- multi-model ensemble voting for documentary artifacts.

This module implements the jury architecture discussed in issues
#92, #106, #107, #79, #81, #83:

    * #92  jury architecture
    * #106 qa_jury.py module
    * #107 consensus aggregation
    * #79  multi-model ensemble
    * #81  prompt-type bias (score normalization via per-voter bias)
    * #83  structural vs semantic QA (free-text union aggregation)

Three concrete voters are provided:

    * ``GeminiVoter``       -- Gemini 3 (Pro or Flash preview), full A/V,
                              family ``"gemini"``, Files API upload.
    * ``DashscopeQwenVoter``-- Qwen3-VL Plus via Dashscope **INTL**
                              OpenAI-compat endpoint, video-only (no audio),
                              family ``"qwen"``, ``score_bias=2.0`` (Qwen
                              runs hot on numeric style ratings).
    * ``GLMVoter``          -- GLM-4.5V via Zhipu OpenAI-compat endpoint,
                              video-only (no audio), family ``"glm"``.

Design rules (enforced here):

    1. **Native video input only.**  Frame sampling is explicitly disallowed.
       If an adapter's API rejects the video input, the voter is marked
       ``disabled`` for that artifact and its verdict is excluded from the
       aggregate -- we do NOT fall back to extracting frames.

    2. **Voters are dropped per-check**: each ``VoterCapabilities`` carries a
       ``cannot_judge`` set.  ``assign_voters(check, available)`` drops voters
       whose ``cannot_judge`` contains the check name, then dedupes by
       ``family`` (at most one voter per family per round).

    3. **Aggregation strategies** (see ``aggregate``):

           * binary    -- majority vote; tie = FAIL (no escalation on tie).
           * numeric   -- median, after subtracting each voter's ``score_bias``
                          from their raw score.
           * free_text -- union across voters with case-insensitive dedupe.

    4. **Confidence** = fraction agreeing with the majority.  Confidence < 0.6
       promotes the overall outcome to ``"escalate"`` (except for explicit
       ties on binary checks, which remain ``"fail"`` per the jury spec).

This module is **not integrated into the pipeline** yet -- that is a separate
workstream (PR7).  Nothing in ``server/agents/``, ``server/orchestrator/``,
``server/callbacks/`` or ``server/tools/assembly_tools.py`` is modified by
this change.
"""

from __future__ import annotations

import asyncio
import logging
import os
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Protocol, Sequence, Union, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical set of audio-dependent checks that video-only voters cannot judge.
# ---------------------------------------------------------------------------
# Kept as a module-level constant so that voter definitions and tests reference
# the same source of truth.
AUDIO_ONLY_CHECKS: frozenset[str] = frozenset(
    {
        "pronunciation",
        "sync",
        "voice_variety",
        "loudness",
        "music",
        "dead_air",
        "dialogue_vs_narration",
    }
)


# ---------------------------------------------------------------------------
# Voter capability metadata
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VoterCapabilities:
    """Static description of what a voter can evaluate.

    Fields
    ------
    native_video:
        True iff the underlying model ingests video natively (no frame
        sampling).  All three adapters in this module set this True.
    native_audio:
        True iff the underlying model ingests audio natively.  Only Gemini
        qualifies today.
    family:
        Coarse family identifier (``"gemini"``, ``"qwen"``, ``"glm"``).  Used
        by :func:`assign_voters` to dedupe voters from the same family.
    cannot_judge:
        Set of check names this voter must NOT be asked to evaluate.  Used
        to drop audio-only checks from video-only voters.
    score_bias:
        Additive bias applied to this voter's numeric scores.  Aggregation
        subtracts ``score_bias`` before taking the median so that a voter
        which is known to inflate (Qwen: +2.0) does not skew the ensemble.
    model_name:
        Concrete model identifier passed to the provider SDK.
    provider:
        Human-readable provider label (``"google"``, ``"dashscope"``,
        ``"zhipu"``).
    """

    native_video: bool
    native_audio: bool
    family: str
    cannot_judge: frozenset[str]
    score_bias: float
    model_name: str
    provider: str


# ---------------------------------------------------------------------------
# Artifact types
# ---------------------------------------------------------------------------
@dataclass
class TTSClip:
    """A single narration audio clip.

    ``reference_text`` is the canonical script line; voters with audio
    support can compare pronunciation / timing against it.
    """

    artifact_id: str
    path: str
    url: str | None = None
    reference_text: str = ""
    voice_role: str = ""
    duration_s: float = 0.0


@dataclass
class VideoClip:
    """One rendered LTX video clip (single visual phrase)."""

    artifact_id: str
    path: str
    url: str | None = None
    visual_phrase: str = ""
    duration_s: float = 0.0


@dataclass
class Scene:
    """An assembled scene (video + synced narration)."""

    artifact_id: str
    path: str
    url: str | None = None
    scene_num: int = 0
    narration_text: str = ""
    visual_description: str = ""
    duration_s: float = 0.0


@dataclass
class FinalCut:
    """The full final documentary, end-to-end."""

    artifact_id: str
    path: str
    url: str | None = None
    full_narration: str = ""
    duration_s: float = 0.0


Artifact = Union[TTSClip, VideoClip, Scene, FinalCut]


# ---------------------------------------------------------------------------
# Voter protocol and verdict structures
# ---------------------------------------------------------------------------
@dataclass
class VoterVerdict:
    """A single voter's response for a single check.

    ``value`` is interpreted by the aggregator according to the
    ``check_type`` passed to :func:`aggregate`:

        * binary    -> ``bool``
        * numeric   -> ``float``
        * free_text -> ``str`` or ``list[str]``

    If ``disabled`` is True the voter's API rejected the artifact (e.g.
    Qwen returned an error for a TTS-only artifact).  Disabled verdicts
    are dropped during aggregation -- frame sampling is NOT used as a
    fallback.
    """

    voter_model: str
    voter_family: str
    voter_score_bias: float
    value: Any
    rationale: str = ""
    disabled: bool = False
    error: str | None = None


@runtime_checkable
class Voter(Protocol):
    """Minimal protocol every voter adapter satisfies."""

    capabilities: VoterCapabilities

    async def judge(self, artifact: Artifact, prompt: str) -> VoterVerdict:  # pragma: no cover - interface only
        ...


@dataclass
class JuryVerdict:
    """Aggregated verdict across all voters for a single check."""

    artifact_id: str
    per_check_results: dict[str, Any] = field(default_factory=dict)
    overall: Literal["pass", "fail", "escalate"] = "pass"
    reasoning: str = ""
    confidence: float = 1.0
    per_voter: list[VoterVerdict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Voter selection
# ---------------------------------------------------------------------------
def assign_voters(check: str, available: Sequence[Voter]) -> list[Voter]:
    """Return the subset of voters eligible to judge ``check``.

    Steps:
        1. Drop voters whose ``cannot_judge`` set contains ``check``.
        2. Dedupe by ``family``: at most one voter per family per round.
           The first voter of a family (in input order) wins.

    The ordering of ``available`` is therefore meaningful -- callers who
    have a preference between two Gemini voters should pass the preferred
    one first.
    """

    eligible = [v for v in available if check not in v.capabilities.cannot_judge]
    seen_families: set[str] = set()
    deduped: list[Voter] = []
    for voter in eligible:
        family = voter.capabilities.family
        if family in seen_families:
            continue
        seen_families.add(family)
        deduped.append(voter)
    return deduped


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
CheckType = Literal["binary", "numeric", "free_text"]

_MIN_CONFIDENCE = 0.6


def _aggregate_binary(
    verdicts: list[VoterVerdict],
    check_name: str,
    artifact_id: str,
) -> JuryVerdict:
    votes = [bool(v.value) for v in verdicts]
    passes = sum(1 for vote in votes if vote)
    fails = len(votes) - passes

    if passes > fails:
        outcome: Literal["pass", "fail", "escalate"] = "pass"
        confidence = passes / len(votes)
    elif fails > passes:
        outcome = "fail"
        confidence = fails / len(votes)
    else:
        # Tie -> FAIL per jury spec; no escalation on ties.
        return JuryVerdict(
            artifact_id=artifact_id,
            per_check_results={check_name: False},
            overall="fail",
            reasoning=f"Binary majority tie ({passes}-{fails}) -> fail.",
            confidence=0.5,
            per_voter=verdicts,
        )

    if confidence < _MIN_CONFIDENCE:
        overall: Literal["pass", "fail", "escalate"] = "escalate"
    else:
        overall = outcome

    return JuryVerdict(
        artifact_id=artifact_id,
        per_check_results={check_name: outcome == "pass"},
        overall=overall,
        reasoning=(
            f"Binary majority: pass={passes} fail={fails} "
            f"(confidence={confidence:.2f}, threshold={_MIN_CONFIDENCE})."
        ),
        confidence=confidence,
        per_voter=verdicts,
    )


def _aggregate_numeric(
    verdicts: list[VoterVerdict],
    check_name: str,
    artifact_id: str,
) -> JuryVerdict:
    # Subtract per-voter score_bias BEFORE taking the median so that an
    # inflationary voter (e.g. Qwen with +2.0) does not distort the ensemble.
    corrected: list[float] = [float(v.value) - float(v.voter_score_bias) for v in verdicts]
    median_value = statistics.median(corrected)

    # Confidence heuristic: tight spread -> high confidence.  We use stddev
    # scaled to a 0-10 rating range; stddev of 0 -> 1.0, stddev >= 5 -> 0.0.
    if len(corrected) > 1:
        stddev = statistics.pstdev(corrected)
    else:
        stddev = 0.0
    confidence = max(0.0, min(1.0, 1.0 - stddev / 5.0))

    overall: Literal["pass", "fail", "escalate"] = (
        "escalate" if confidence < _MIN_CONFIDENCE else "pass"
    )

    return JuryVerdict(
        artifact_id=artifact_id,
        per_check_results={check_name: median_value},
        overall=overall,
        reasoning=(
            f"Numeric median (bias-corrected): {median_value:.2f} over "
            f"{len(corrected)} voters; stddev={stddev:.2f}, "
            f"confidence={confidence:.2f}."
        ),
        confidence=confidence,
        per_voter=verdicts,
    )


def _aggregate_free_text(
    verdicts: list[VoterVerdict],
    check_name: str,
    artifact_id: str,
) -> JuryVerdict:
    seen: set[str] = set()
    union: list[str] = []
    for verdict in verdicts:
        items: Iterable[str]
        if isinstance(verdict.value, (list, tuple, set)):
            items = [str(x) for x in verdict.value]
        else:
            items = [str(verdict.value)]
        for item in items:
            trimmed = item.strip()
            if not trimmed:
                continue
            key = trimmed.lower()
            if key in seen:
                continue
            seen.add(key)
            union.append(trimmed)

    return JuryVerdict(
        artifact_id=artifact_id,
        per_check_results={check_name: union},
        overall="pass",
        reasoning=(
            f"Free-text union of {len(verdicts)} voters -> {len(union)} "
            f"unique items."
        ),
        confidence=1.0,
        per_voter=verdicts,
    )


def aggregate(
    verdicts: Sequence[VoterVerdict],
    check_type: CheckType,
    *,
    check_name: str = "check",
    artifact_id: str = "",
) -> JuryVerdict:
    """Aggregate voter verdicts for a single check.

    Parameters
    ----------
    verdicts:
        The per-voter verdicts for a single check.  ``disabled`` verdicts
        are dropped first -- frame-sampling fallback is explicitly NOT used.
    check_type:
        ``"binary"``, ``"numeric"``, or ``"free_text"``.
    check_name:
        The logical name of the check; embedded in the returned
        ``per_check_results`` dict for convenience.
    artifact_id:
        Identifier of the artifact being judged.
    """

    valid = [v for v in verdicts if not v.disabled and v.error is None]
    if not valid:
        return JuryVerdict(
            artifact_id=artifact_id,
            per_check_results={},
            overall="escalate",
            reasoning="No eligible voter verdicts (all disabled or errored).",
            confidence=0.0,
            per_voter=list(verdicts),
        )

    if check_type == "binary":
        return _aggregate_binary(valid, check_name, artifact_id)
    if check_type == "numeric":
        return _aggregate_numeric(valid, check_name, artifact_id)
    if check_type == "free_text":
        return _aggregate_free_text(valid, check_name, artifact_id)
    raise ValueError(f"Unknown check_type: {check_type!r}")


# ---------------------------------------------------------------------------
# Voter implementations
# ---------------------------------------------------------------------------
class GeminiVoter:
    """Native A/V voter via Google's google-genai SDK.

    Uses the Files API for upload so both audio and video are ingested
    natively -- no frame sampling, no transcription round-trip.

    The default ``model_name`` is ``"gemini-3-pro-preview"``; pass
    ``"gemini-3-flash-preview"`` for the cheaper Flash variant when latency
    or cost dominates.
    """

    def __init__(
        self,
        model_name: str = "gemini-3-pro-preview",
        *,
        api_key_env: str = "GOOGLE_API_KEY",
        request_timeout_s: float = 180.0,
    ) -> None:
        self._model_name = model_name
        self._api_key_env = api_key_env
        self._request_timeout_s = request_timeout_s
        self.capabilities = VoterCapabilities(
            native_video=True,
            native_audio=True,
            family="gemini",
            cannot_judge=frozenset(),
            score_bias=0.0,
            model_name=model_name,
            provider="google",
        )

    def _client(self):
        # Imported lazily so that simply importing qa_jury does not pull
        # google-genai into every import graph in the repo.
        from google import genai  # type: ignore

        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise RuntimeError(
                f"{self._api_key_env} is not set; GeminiVoter cannot run."
            )
        return genai.Client(api_key=api_key)

    async def judge(self, artifact: Artifact, prompt: str) -> VoterVerdict:
        return await asyncio.to_thread(self._judge_sync, artifact, prompt)

    def _judge_sync(self, artifact: Artifact, prompt: str) -> VoterVerdict:
        try:
            client = self._client()
            uploaded = client.files.upload(file=artifact.path)

            # Wait for the file to reach ACTIVE state before generate_content.
            # Gemini Files API returns PROCESSING for a few seconds on video.
            deadline = time.monotonic() + self._request_timeout_s
            while getattr(uploaded, "state", None) and str(uploaded.state).endswith("PROCESSING"):
                if time.monotonic() > deadline:
                    return VoterVerdict(
                        voter_model=self._model_name,
                        voter_family="gemini",
                        voter_score_bias=0.0,
                        value=None,
                        disabled=True,
                        error="Files API upload did not reach ACTIVE state in time.",
                    )
                time.sleep(2.0)
                uploaded = client.files.get(name=uploaded.name)

            response = client.models.generate_content(
                model=self._model_name,
                contents=[uploaded, prompt],
            )
            text = getattr(response, "text", "") or ""
            return VoterVerdict(
                voter_model=self._model_name,
                voter_family="gemini",
                voter_score_bias=0.0,
                value=text,
                rationale=text,
            )
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            logger.warning("GeminiVoter failed on %s: %s", artifact.artifact_id, exc)
            return VoterVerdict(
                voter_model=self._model_name,
                voter_family="gemini",
                voter_score_bias=0.0,
                value=None,
                disabled=True,
                error=str(exc),
            )


class _OpenAICompatVideoVoter:
    """Shared implementation for video-only OpenAI-compatible voters.

    Both Dashscope (Qwen3-VL) and Zhipu (GLM-4.5V) expose an
    OpenAI-compatible chat-completions endpoint that accepts the custom
    ``video_url`` content type.  This base class handles the common path;
    subclasses only supply ``base_url``, ``api_key_env``, and capability
    metadata.

    Per the jury spec: if the provider rejects the video input we mark the
    verdict disabled -- we do NOT fall back to frame sampling.
    """

    capabilities: VoterCapabilities

    def __init__(
        self,
        model_name: str,
        base_url: str,
        api_key_env: str,
        *,
        request_timeout_s: float = 180.0,
    ) -> None:
        self._model_name = model_name
        self._base_url = base_url
        self._api_key_env = api_key_env
        self._request_timeout_s = request_timeout_s

    def _client(self):
        from openai import OpenAI  # type: ignore

        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise RuntimeError(
                f"{self._api_key_env} is not set; {type(self).__name__} cannot run."
            )
        return OpenAI(api_key=api_key, base_url=self._base_url)

    async def judge(self, artifact: Artifact, prompt: str) -> VoterVerdict:
        return await asyncio.to_thread(self._judge_sync, artifact, prompt)

    def _judge_sync(self, artifact: Artifact, prompt: str) -> VoterVerdict:
        if not artifact.url:
            return VoterVerdict(
                voter_model=self._model_name,
                voter_family=self.capabilities.family,
                voter_score_bias=self.capabilities.score_bias,
                value=None,
                disabled=True,
                error=(
                    "OpenAI-compatible video voters require a public URL on "
                    "the artifact; no frame-sampling fallback is used."
                ),
            )

        try:
            client = self._client()
            response = client.chat.completions.create(
                model=self._model_name,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "video_url", "video_url": {"url": artifact.url}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                timeout=self._request_timeout_s,
            )
            text = response.choices[0].message.content if response.choices else ""
            return VoterVerdict(
                voter_model=self._model_name,
                voter_family=self.capabilities.family,
                voter_score_bias=self.capabilities.score_bias,
                value=text,
                rationale=text or "",
            )
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            logger.warning(
                "%s failed on %s: %s", type(self).__name__, artifact.artifact_id, exc
            )
            return VoterVerdict(
                voter_model=self._model_name,
                voter_family=self.capabilities.family,
                voter_score_bias=self.capabilities.score_bias,
                value=None,
                disabled=True,
                error=str(exc),
            )


class DashscopeQwenVoter(_OpenAICompatVideoVoter):
    """Qwen3-VL Plus via the Dashscope **international** endpoint.

    IMPORTANT: the international base URL
    ``https://dashscope-intl.aliyuncs.com/compatible-mode/v1`` is
    mandatory.  The China endpoint rejects international API keys.
    """

    _BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

    def __init__(
        self,
        model_name: str = "qwen3-vl-plus",
        *,
        api_key_env: str = "DASHSCOPE_INTL_API_KEY",
        request_timeout_s: float = 180.0,
    ) -> None:
        super().__init__(
            model_name=model_name,
            base_url=self._BASE_URL,
            api_key_env=api_key_env,
            request_timeout_s=request_timeout_s,
        )
        self.capabilities = VoterCapabilities(
            native_video=True,
            native_audio=False,
            family="qwen",
            cannot_judge=AUDIO_ONLY_CHECKS,
            score_bias=2.0,
            model_name=model_name,
            provider="dashscope",
        )


class GLMVoter(_OpenAICompatVideoVoter):
    """GLM-4.5V via Zhipu's OpenAI-compat endpoint."""

    _BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

    def __init__(
        self,
        model_name: str = "glm-4.5v",
        *,
        api_key_env: str = "GLM_API_KEY",
        request_timeout_s: float = 180.0,
    ) -> None:
        super().__init__(
            model_name=model_name,
            base_url=self._BASE_URL,
            api_key_env=api_key_env,
            request_timeout_s=request_timeout_s,
        )
        self.capabilities = VoterCapabilities(
            native_video=True,
            native_audio=False,
            family="glm",
            cannot_judge=AUDIO_ONLY_CHECKS,
            score_bias=0.0,
            model_name=model_name,
            provider="zhipu",
        )


# ---------------------------------------------------------------------------
# Optional natural-language reasoning synthesis (one extra Gemini Flash call)
# ---------------------------------------------------------------------------
async def summarize_reasoning(
    jury_verdict: JuryVerdict,
    *,
    model_name: str = "gemini-3-flash-preview",
    api_key_env: str = "GOOGLE_API_KEY",
) -> str:
    """Produce a natural-language explanation summarizing a JuryVerdict.

    This is the "one extra Gemini Flash call" referenced in issue #107.
    The call is best-effort: on any error we return the deterministic
    reasoning string already on the verdict, so the pipeline never depends
    on this for correctness.
    """

    try:
        from google import genai  # type: ignore

        api_key = os.environ.get(api_key_env)
        if not api_key:
            return jury_verdict.reasoning

        per_voter_lines = []
        for v in jury_verdict.per_voter:
            marker = "DISABLED" if v.disabled else "OK"
            per_voter_lines.append(
                f"- [{marker}] {v.voter_model} ({v.voter_family}, "
                f"bias={v.voter_score_bias:+.1f}): {v.value!r}"
            )

        prompt = (
            "Summarize the following QA jury verdict in one short paragraph "
            "suitable for a pipeline log line. Be factual; do not speculate.\n\n"
            f"Artifact: {jury_verdict.artifact_id}\n"
            f"Overall: {jury_verdict.overall}\n"
            f"Confidence: {jury_verdict.confidence:.2f}\n"
            f"Deterministic reasoning: {jury_verdict.reasoning}\n"
            f"Per-voter verdicts:\n" + "\n".join(per_voter_lines)
        )

        def _call() -> str:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=model_name,
                contents=[prompt],
            )
            return (getattr(response, "text", "") or "").strip()

        text = await asyncio.to_thread(_call)
        return text or jury_verdict.reasoning
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.debug("summarize_reasoning Gemini Flash call failed: %s", exc)
        return jury_verdict.reasoning


__all__ = [
    "AUDIO_ONLY_CHECKS",
    "Artifact",
    "CheckType",
    "DashscopeQwenVoter",
    "FinalCut",
    "GLMVoter",
    "GeminiVoter",
    "JuryVerdict",
    "Scene",
    "TTSClip",
    "VideoClip",
    "Voter",
    "VoterCapabilities",
    "VoterVerdict",
    "aggregate",
    "assign_voters",
    "summarize_reasoning",
]
