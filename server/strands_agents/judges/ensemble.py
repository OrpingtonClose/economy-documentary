"""Judge ensemble — routing, voting, and proprietary-API fallback.

The ensemble is the single façade every Tier-2 eval calls into.  It
hides three operational concerns from evaluators:

1. **Role routing.**  Evaluators ask for ``query_safety`` / ``query_av``
   / ``query_tiebreaker``; the ensemble picks the right local client
   from its registry.  Evaluators never name models directly.
2. **Voting.**  AV decisions benefit from a tiebreaker.  Safety calls
   do not — abliterated Gemma 4 is the authority and a second judge
   would just dilute its verdict.  The ensemble encodes that policy so
   evaluators can't accidentally under- or over-vote.
3. **Fallback.**  Proprietary APIs (Claude, GPT, Gemini) are only used
   as a last-resort tiebreaker when both local AV judges fail *or*
   their verdicts disagree past :attr:`JudgeEnsemble.disagreement_threshold`.
   Every call path records whether the fallback fired, so traces
   surface proprietary-API reliance without hiding it.

The ensemble never raises on judge failure.  A judge that returns
``ok=False`` is treated as abstaining, not as a hard error — otherwise
the evaluator loses the other judges' verdicts too.

Wire shape: judges are expected to emit JSON-parseable verdicts shaped
like ``{"score": <float 0..1>, "verdict": "<label>", "reasoning": ...}``.
The ensemble parses defensively — a judge that returns prose gets a
``score=None`` verdict and is counted as an abstention.  This makes the
ensemble compatible with models that occasionally drop out of JSON mode.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from strands_agents.judges.client import JudgeClient, JudgeRequest, JudgeResponse
from strands_agents.judges.models import JudgeRole

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Verdict types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeVerdict:
    """Single-judge verdict — raw response plus parsed score.

    Attributes:
        model: Model identifier the client reported.  Kept so the
            ensemble trace can tell ``qwen`` from ``salmonn`` even when
            two clients are wired to the same role.
        role: Role the client played for this call
            (``safety`` / ``av_primary`` / ``av_tiebreaker`` /
            ``fallback``).
        ok: Whether the underlying judge returned successfully.  False
            when the client returned an error envelope.
        score: Parsed numeric score in ``[0.0, 1.0]`` or ``None`` if
            the judge didn't emit a parseable score (prose-only
            response, malformed JSON, dropout).  Evaluators treat
            ``None`` as abstention.
        verdict: Optional string label (``"pass"`` / ``"fail"`` /
            ``"escalate"`` / ...).  Free-form — the rubric decides.
        reasoning: Judge-provided justification.  Surfaced in traces
            but not used for arithmetic.
        raw: Raw response text from the judge.  Kept for debugging
            rubric regressions without having to re-run the judge.
        error: Populated when :attr:`ok` is False.
    """

    model: str
    role: str
    ok: bool
    score: Optional[float] = None
    verdict: str = ""
    reasoning: str = ""
    raw: str = ""
    error: str = ""


@dataclass(frozen=True)
class EnsembleVerdict:
    """Aggregated verdict after routing to the role's judge set.

    Attributes:
        score: Consensus score in ``[0.0, 1.0]`` (mean of the
            contributing judges' scores).  ``None`` if no judge
            produced a parseable score — evaluators treat this as an
            abstention and typically fall back to mechanistic checks.
        verdict: The majority-vote verdict string, or empty when no
            majority.
        agreed: True if every non-abstaining judge's verdict string
            matched.  False means the ensemble used the tiebreaker or
            the fallback.
        used_fallback: True if the proprietary-API fallback judge was
            invoked (either because all locals failed, or because they
            disagreed past :attr:`JudgeEnsemble.disagreement_threshold`).
            Surfaced so dashboards can track proprietary-API reliance.
        verdicts: Per-judge verdicts, in call order.  Kept for trace
            inspection and for evaluators that want to vote differently
            than the default (e.g. safety evals that need every
            judge's individual score).
    """

    score: Optional[float]
    verdict: str
    agreed: bool
    used_fallback: bool
    verdicts: tuple[JudgeVerdict, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """True if at least one judge produced a parseable score."""

        return self.score is not None


# ---------------------------------------------------------------------------
# Parser — lenient JSON extraction from judge text
# ---------------------------------------------------------------------------

# Matches the first {...} block in the response.  Used when the judge
# surrounds its JSON with reasoning prose (common for Gemma-family).
_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _parse_judge_text(text: str) -> tuple[Optional[float], str, str]:
    """Pull ``(score, verdict, reasoning)`` from a judge's raw text.

    The parser accepts:

    - Pure JSON: ``{"score": 0.8, "verdict": "pass", "reasoning": "..."}``.
    - JSON wrapped in prose.
    - Prose-only (returns ``(None, "", text)``).

    Numeric scores outside ``[0.0, 1.0]`` are clamped.  A missing
    ``verdict`` key defaults to ``""`` — evaluators needing a label
    must check for the empty string.

    Returns:
        ``(score, verdict, reasoning)`` tuple.
    """

    candidate = _extract_json_blob(text)
    if candidate is None:
        return None, "", text.strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None, "", text.strip()

    if not isinstance(parsed, dict):
        return None, "", text.strip()

    raw_score = parsed.get("score")
    score: Optional[float]
    if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
        score = max(0.0, min(1.0, float(raw_score)))
    else:
        score = None

    verdict_raw = parsed.get("verdict", "")
    verdict = str(verdict_raw).strip().lower() if verdict_raw is not None else ""

    reasoning_raw = parsed.get("reasoning", "")
    reasoning = str(reasoning_raw) if reasoning_raw is not None else ""

    return score, verdict, reasoning


def _extract_json_blob(text: str) -> Optional[str]:
    """Return the most likely JSON object substring from ``text``.

    Prefers ``text`` itself if it parses as JSON; otherwise returns the
    first ``{...}`` block matched by :data:`_JSON_BLOCK_RE`.  Nested
    objects aren't extracted by the regex — judges that wrap their
    verdict in another dict need to flatten before the call reaches
    here.

    The structural ``startswith``/``endswith`` check isn't sufficient
    on its own: a judge response like
    ``{"score": 0.8}\\nsome notes about {the analysis}`` satisfies
    both but isn't a single parseable object.  We verify parseability
    before returning the full text; a parse failure falls through to
    the regex so the first non-nested JSON block still wins.
    """

    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            return stripped
    match = _JSON_BLOCK_RE.search(stripped)
    if match is None:
        return None
    return match.group(0)


# ---------------------------------------------------------------------------
# Ensemble
# ---------------------------------------------------------------------------


_LOCAL_ROLES: tuple[JudgeRole, ...] = ("safety", "av_primary", "av_tiebreaker")


class JudgeEnsemble:
    """Routes :class:`JudgeRequest` s to the right judge(s) and aggregates verdicts.

    Clients are registered by role.  Missing roles are tolerated — a
    deployment that hasn't stood up the tiebreaker yet can still run
    safety + av_primary evals.  The ensemble only errors when an
    explicitly-requested role has no client AND no fallback.

    Args:
        clients: Map of role → :class:`JudgeClient`.  Keys must be one
            of the values in :data:`strands_agents.judges.models.JudgeRole`.
        fallback: Optional client used when:

            - every local judge for a call failed, OR
            - local judges produced conflicting scores whose spread
              exceeded :attr:`disagreement_threshold`.

            Intended for proprietary-API clients (Claude, Gemini,
            GPT-5) — keep it ``None`` in CI to enforce local-only
            evaluation.
        disagreement_threshold: Absolute-difference threshold on scores
            above which the ensemble considers AV judges in conflict
            and invokes the fallback.  Defaults to ``0.3`` — generous
            enough that matching-verdict / close-score cases don't
            spuriously trigger fallback while still catching real
            splits.

    Raises:
        ValueError: If ``clients`` contains a key that's not a valid
            role.
    """

    def __init__(
        self,
        clients: dict[JudgeRole, JudgeClient],
        *,
        fallback: Optional[JudgeClient] = None,
        disagreement_threshold: float = 0.3,
    ) -> None:
        for role in clients:
            if role not in _LOCAL_ROLES:
                raise ValueError(
                    f"unknown judge role {role!r}; expected one of {_LOCAL_ROLES}"
                )
        if not 0.0 <= disagreement_threshold <= 1.0:
            raise ValueError(
                f"disagreement_threshold must be in [0, 1], got {disagreement_threshold!r}"
            )
        self._clients: dict[JudgeRole, JudgeClient] = dict(clients)
        self._fallback = fallback
        self.disagreement_threshold = disagreement_threshold

    # ------------------------------------------------------------------
    # Routing helpers
    # ------------------------------------------------------------------

    def has_role(self, role: JudgeRole) -> bool:
        """Return True if a client is registered for ``role``.

        Used by evaluators to skip tiebreaker passes on deployments
        that haven't spun up the full fleet yet.
        """

        return role in self._clients

    @property
    def fallback_available(self) -> bool:
        """True if a proprietary-API fallback client is registered."""

        return self._fallback is not None

    def close(self) -> None:
        """Release resources held by every registered client.

        Safe to call multiple times.
        """

        for client in self._clients.values():
            client.close()
        if self._fallback is not None:
            self._fallback.close()

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def query_safety(self, request: JudgeRequest) -> EnsembleVerdict:
        """Route to the safety judge (Gemma 4 abliterated).

        Single-judge by design.  If the abliterated safety judge is
        unavailable we do NOT fall back to a proprietary API — a
        refusal-trained judge would skew safety evals in the wrong
        direction.  A missing safety client returns an empty verdict
        instead, so the caller can decide whether to fail the test or
        defer.
        """

        client = self._clients.get("safety")
        if client is None:
            logger.warning(
                "role=<safety>, state=<no_client> | safety judge not configured, returning abstention"
            )
            return EnsembleVerdict(
                score=None,
                verdict="",
                agreed=True,
                used_fallback=False,
                verdicts=(),
            )

        verdict = _call_and_parse(client, request, role="safety")
        return EnsembleVerdict(
            score=verdict.score,
            verdict=verdict.verdict,
            agreed=True,
            used_fallback=False,
            verdicts=(verdict,),
        )

    def query_av(self, request: JudgeRequest) -> EnsembleVerdict:
        """Route to the AV judges (Qwen3.5-Omni primary + SALMONN tiebreaker).

        Both judges are called; the tiebreaker is NOT called
        conditionally because its verdict is informative even when the
        primary passes — evaluators aggregate per-scene disagreement
        metrics over time.

        Falls back to the proprietary client when:

        - both local AV judges return ``ok=False``, OR
        - the primary and tiebreaker produce parseable scores whose
          absolute difference exceeds
          :attr:`disagreement_threshold`.
        """

        verdicts: list[JudgeVerdict] = []

        primary = self._clients.get("av_primary")
        if primary is not None:
            verdicts.append(_call_and_parse(primary, request, role="av_primary"))

        tiebreaker = self._clients.get("av_tiebreaker")
        if tiebreaker is not None:
            verdicts.append(_call_and_parse(tiebreaker, request, role="av_tiebreaker"))

        return self._aggregate_with_fallback(request, verdicts)

    def query_av_primary(self, request: JudgeRequest) -> EnsembleVerdict:
        """Route to the AV primary only (Qwen3.5-Omni).

        Skips the tiebreaker.  Used by evaluators that grade coarse
        per-scene AV quality where the extra judge would be wasted
        spend.  Falls back to the proprietary client when the local
        primary fails.
        """

        primary = self._clients.get("av_primary")
        verdicts: list[JudgeVerdict] = []
        if primary is not None:
            verdicts.append(_call_and_parse(primary, request, role="av_primary"))

        return self._aggregate_with_fallback(request, verdicts)

    def query_tiebreaker(self, request: JudgeRequest) -> EnsembleVerdict:
        """Invoke only the tiebreaker judge (video-SALMONN 2 72B).

        Used by evaluators that already have a primary verdict and
        want the second opinion in isolation — the ensemble's
        agreement bookkeeping is skipped here.
        """

        tiebreaker = self._clients.get("av_tiebreaker")
        if tiebreaker is None:
            logger.warning(
                "role=<av_tiebreaker>, state=<no_client> | tiebreaker not configured, returning abstention"
            )
            return EnsembleVerdict(
                score=None,
                verdict="",
                agreed=True,
                used_fallback=False,
                verdicts=(),
            )
        verdict = _call_and_parse(tiebreaker, request, role="av_tiebreaker")
        return EnsembleVerdict(
            score=verdict.score,
            verdict=verdict.verdict,
            agreed=True,
            used_fallback=False,
            verdicts=(verdict,),
        )

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _aggregate_with_fallback(
        self,
        request: JudgeRequest,
        verdicts: list[JudgeVerdict],
    ) -> EnsembleVerdict:
        """Aggregate ``verdicts`` and optionally invoke the fallback.

        Fallback fires when either:

        1. every local verdict is unusable (``ok=False`` or unparseable
           score) AND a fallback is registered, OR
        2. two local verdicts have parseable scores whose absolute
           difference exceeds :attr:`disagreement_threshold` AND a
           fallback is registered.

        Returns the aggregated :class:`EnsembleVerdict`.  When the
        fallback was invoked, its verdict is appended to
        :attr:`EnsembleVerdict.verdicts` and
        :attr:`EnsembleVerdict.used_fallback` is True.
        """

        parseable = [v for v in verdicts if v.score is not None]
        all_failed = len(verdicts) == 0 or all(not v.ok for v in verdicts)

        should_fallback = False
        if self._fallback is not None:
            if all_failed or len(parseable) == 0:
                should_fallback = True
            elif len(parseable) >= 2:
                spread = max(v.score for v in parseable) - min(  # type: ignore[type-var]
                    v.score for v in parseable  # type: ignore[type-var]
                )
                if spread > self.disagreement_threshold:
                    should_fallback = True

        if should_fallback:
            assert self._fallback is not None
            fb_verdict = _call_and_parse(self._fallback, request, role="fallback")
            verdicts = [*verdicts, fb_verdict]
            if fb_verdict.score is not None:
                parseable = [*parseable, fb_verdict]
            logger.info(
                "role=<fallback>, model=<%s>, parseable_count=<%d> | proprietary fallback invoked",
                fb_verdict.model,
                len(parseable),
            )

        score = _mean_or_none([v.score for v in parseable if v.score is not None])

        non_empty_verdicts = [v.verdict for v in parseable if v.verdict]
        if non_empty_verdicts and len(set(non_empty_verdicts)) == 1:
            consensus_verdict = non_empty_verdicts[0]
            agreed = True
        else:
            consensus_verdict = _majority_vote(non_empty_verdicts)
            agreed = False if len(set(non_empty_verdicts)) > 1 else True

        return EnsembleVerdict(
            score=score,
            verdict=consensus_verdict,
            agreed=agreed,
            used_fallback=should_fallback,
            verdicts=tuple(verdicts),
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _call_and_parse(
    client: JudgeClient,
    request: JudgeRequest,
    *,
    role: str,
) -> JudgeVerdict:
    """Invoke ``client.complete`` and parse the result into a verdict.

    Never raises — a client that explodes unexpectedly is logged and
    returned as ``ok=False``.  Keeping this invariant means the
    ensemble's aggregation logic can treat every path uniformly.
    """

    try:
        response: JudgeResponse = client.complete(request)
    except Exception as exc:  # pragma: no cover — defensive, clients shouldn't raise
        logger.warning(
            "role=<%s>, error=<%s> | judge client raised unexpectedly",
            role,
            exc,
        )
        return JudgeVerdict(
            model="",
            role=role,
            ok=False,
            error=str(exc),
        )

    if not response.ok:
        return JudgeVerdict(
            model=response.model,
            role=role,
            ok=False,
            raw=response.text,
            error=response.error,
        )

    score, verdict, reasoning = _parse_judge_text(response.text)
    return JudgeVerdict(
        model=response.model,
        role=role,
        ok=True,
        score=score,
        verdict=verdict,
        reasoning=reasoning,
        raw=response.text,
    )


def _mean_or_none(values: list[float]) -> Optional[float]:
    """Return the mean of ``values`` or None when the list is empty."""

    if not values:
        return None
    return sum(values) / len(values)


def _majority_vote(labels: list[str]) -> str:
    """Return the most common label, or "" on tie / empty input.

    Ties resolve to "" rather than to a stable pick because the
    ensemble's ``agreed`` flag already conveys "no consensus" —
    returning an arbitrary winner would silently mask the split.
    """

    if not labels:
        return ""
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    top_count = max(counts.values())
    winners = [label for label, count in counts.items() if count == top_count]
    if len(winners) != 1:
        return ""
    return winners[0]
