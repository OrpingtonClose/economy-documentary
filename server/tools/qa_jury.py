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


# ---------------------------------------------------------------------------
# Voter implementations
# ---------------------------------------------------------------------------


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

            # OpenAI chat-completions defines ``message.content`` as
            # ``Optional[str]``.  A ``None`` here would silently coerce to
            # ``bool(None) == False`` in binary aggregation or crash
            # ``float(None)`` in numeric aggregation, so we treat an empty
            # response the same way we treat a rejected video: mark the
            # verdict disabled and let ``aggregate`` drop it.
            raw_text: str | None = None
            if response.choices:
                raw_text = response.choices[0].message.content
            if not raw_text:
                return VoterVerdict(
                    voter_model=self._model_name,
                    voter_family=self.capabilities.family,
                    voter_score_bias=self.capabilities.score_bias,
                    value=None,
                    disabled=True,
                    error="Provider returned an empty response (no message content).",
                )
            return VoterVerdict(
                voter_model=self._model_name,
                voter_family=self.capabilities.family,
                voter_score_bias=self.capabilities.score_bias,
                value=raw_text,
                rationale=raw_text,
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


# ---------------------------------------------------------------------------
# Optional natural-language reasoning synthesis (one extra Gemini Flash call)
# ---------------------------------------------------------------------------


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
