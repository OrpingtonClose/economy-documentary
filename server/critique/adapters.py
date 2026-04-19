"""Opt-in converters from existing evaluator outputs to the unified record.

The critique store is new infrastructure; the pipeline already emits QA
verdicts through :mod:`tools.qa_jury`, :mod:`gatekeeper`,
:mod:`callbacks.timeline_guardian`, :mod:`tools.scenario_evaluator_checks`
and the visual ``coherence_evaluator``.  Rather than rewriting every
caller, these adapters translate each system's native output shape to a
:class:`critique.record.QaVerdict` (or :class:`Critique` where
appropriate) so pipeline code can mirror its existing verdict into the
store with a one-liner.

PR-1 does **not** wire these into the live pipeline — the adapters exist
as a stable seam so PR-2 (pull-based supervisor) and PR-3 (critic squads)
have a single type to target.  Keeping them separate from the dataclasses
also means :mod:`critique.record` stays dependency-free of the
evaluators it describes.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from critique.record import (
    Critique,
    CritiqueRating,
    QaVerdict,
    QaVerdictStatus,
)


# ---------------------------------------------------------------------------
# Rating ↔ verdict helpers
# ---------------------------------------------------------------------------

# Canonical mapping from the EXCELLENT/GOOD/FAIR/POOR scale used by the
# scenario + coherence evaluators into the QA verdict scale.  FAIR is
# treated as ``warn`` (not blocking but worth flagging) to match the
# existing LoopAgent exit condition (GOOD or EXCELLENT → exit_loop).
_RATING_TO_VERDICT: dict[str, QaVerdictStatus] = {
    "EXCELLENT": "pass",
    "GOOD": "pass",
    "FAIR": "warn",
    "POOR": "fail",
    "UNKNOWN": "escalate",
}


def rating_to_verdict(rating: str) -> QaVerdictStatus:
    """Normalise an evaluator rating string to a :data:`QaVerdictStatus`.

    Unknown ratings become ``"escalate"`` rather than silently passing.
    """

    return _RATING_TO_VERDICT.get((rating or "").strip().upper(), "escalate")


def rating_to_critique_rating(rating: str) -> CritiqueRating:
    """Normalise an evaluator rating string to :data:`CritiqueRating`."""

    normalised = (rating or "").strip().upper()
    if normalised in ("EXCELLENT", "GOOD", "FAIR", "POOR"):
        return normalised  # type: ignore[return-value]
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# qa_jury
# ---------------------------------------------------------------------------

def jury_to_qa(
    jury_verdict: Any,
    *,
    check_name: str = "",
    source: str = "qa_jury",
) -> QaVerdict:
    """Convert a :class:`tools.qa_jury.JuryVerdict` to a :class:`QaVerdict`.

    ``JuryVerdict`` is imported lazily (keeping this module dependency-
    free) and accessed by attribute so this adapter works against duck-
    typed stand-ins in tests.  ``per_check_results`` is preserved in
    ``details`` for callers that want to drill in.
    """

    overall = getattr(jury_verdict, "overall", "pass")
    if overall not in ("pass", "fail", "escalate"):
        overall = "escalate"
    # JuryVerdict emits ``pass``/``fail``/``escalate``; the unified
    # schema has an extra ``warn`` level, unused here.
    confidence = float(getattr(jury_verdict, "confidence", 1.0) or 0.0)
    reasoning = str(getattr(jury_verdict, "reasoning", "") or "")
    per_check = getattr(jury_verdict, "per_check_results", {}) or {}
    artifact_id = str(getattr(jury_verdict, "artifact_id", "") or "")

    resolved_check = check_name or (next(iter(per_check.keys()), "") if per_check else "jury")
    return QaVerdict(
        source=source,
        check_name=resolved_check or "jury",
        verdict=overall,
        confidence=max(0.0, min(1.0, confidence)),
        rating=None,
        message=reasoning,
        details={
            "per_check_results": dict(per_check),
            "artifact_id": artifact_id,
        },
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# gatekeeper
# ---------------------------------------------------------------------------

def gatekeeper_to_qa(check: Any) -> QaVerdict:
    """Convert a :class:`gatekeeper.GatekeeperCheck` to a :class:`QaVerdict`.

    ``check.verdict`` is either the :class:`gatekeeper.GatekeeperVerdict`
    enum or its string value (``"pass"``/``"warn"``/``"reject"``).
    ``reject`` maps to the unified ``fail`` status.
    """

    raw_verdict = getattr(check, "verdict", "pass")
    verdict_str = getattr(raw_verdict, "value", raw_verdict)
    if verdict_str == "reject":
        verdict: QaVerdictStatus = "fail"
    elif verdict_str in ("pass", "warn", "escalate", "fail"):
        verdict = verdict_str
    else:
        verdict = "escalate"

    return QaVerdict(
        source="gatekeeper",
        check_name=str(getattr(check, "name", "gatekeeper")),
        verdict=verdict,
        confidence=1.0,
        rating=None,
        message=str(getattr(check, "message", "")),
        details={
            "category": str(getattr(check, "category", "")),
            "stage": str(getattr(check, "stage", "")),
            "scene_num": int(getattr(check, "scene_num", 0) or 0),
            "phrase_idx": int(getattr(check, "phrase_idx", 0) or 0),
            "metadata": dict(getattr(check, "metadata", {}) or {}),
        },
        timestamp=float(getattr(check, "timestamp", 0.0) or time.time()),
    )


# ---------------------------------------------------------------------------
# timeline_guardian
# ---------------------------------------------------------------------------

def timeline_guardian_to_qa(
    check_name: str,
    passed: bool,
    *,
    message: str = "",
    details: Optional[dict[str, Any]] = None,
) -> QaVerdict:
    """Wrap a :mod:`callbacks.timeline_guardian` assertion in a QA verdict.

    ``timeline_guardian`` currently raises/returns booleans rather than
    producing a structured object, so this helper is deliberately
    minimal — callers pass their check name and outcome directly.
    """

    return QaVerdict(
        source="timeline_guardian",
        check_name=check_name,
        verdict="pass" if passed else "fail",
        confidence=1.0,
        rating=None,
        message=message,
        details=dict(details or {}),
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# scenario evaluator
# ---------------------------------------------------------------------------

def scenario_evaluator_to_qa(
    rating: str,
    *,
    structural_cap: str = "",
    report: str = "",
    check_name: str = "scenario_adhd",
) -> QaVerdict:
    """Convert scenario evaluator output to a :class:`QaVerdict`.

    ``rating`` is one of ``EXCELLENT`` / ``GOOD`` / ``FAIR`` / ``POOR``.
    ``structural_cap`` (from
    :mod:`tools.scenario_evaluator_checks`) caps the verdict at POOR
    when structural checks fail; callers who have already applied the
    cap pass the final rating here.
    """

    verdict = rating_to_verdict(rating)
    return QaVerdict(
        source="scenario_evaluator",
        check_name=check_name,
        verdict=verdict,
        confidence=1.0,
        rating=(rating or "").strip().upper() or None,
        message=report,
        details={"structural_cap": (structural_cap or "").strip().upper()},
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# coherence evaluator (visual director)
# ---------------------------------------------------------------------------

def coherence_evaluator_to_qa(
    rating: str,
    *,
    rationale: str = "",
    check_name: str = "visual_coherence",
) -> QaVerdict:
    """Convert visual ``coherence_evaluator`` output to a :class:`QaVerdict`."""

    verdict = rating_to_verdict(rating)
    return QaVerdict(
        source="coherence_evaluator",
        check_name=check_name,
        verdict=verdict,
        confidence=1.0,
        rating=(rating or "").strip().upper() or None,
        message=rationale,
        details={},
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# Critic-squad convenience
# ---------------------------------------------------------------------------

def critic_payload_to_critique(
    payload: dict[str, Any],
    *,
    source: str,
    voter_model: str = "",
) -> Critique:
    """Build a :class:`Critique` from a critic agent's structured output.

    Critic squads (PR-3) will emit JSON-ish payloads with a free mix of
    ``rating`` / ``score`` / ``summary`` / ``issues`` / ``suggestions``
    fields.  This adapter tolerates missing keys and coerces types so
    brittle agents cannot crash the critique pipeline.
    """

    rating_raw = payload.get("rating", payload.get("verdict", ""))
    rating = rating_to_critique_rating(str(rating_raw or ""))

    raw_score = payload.get("score")
    score: Optional[float]
    try:
        score = float(raw_score) if raw_score is not None else None
    except (TypeError, ValueError):
        score = None

    def _as_str_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        try:
            return [str(v) for v in value if str(v).strip()]
        except TypeError:
            return [str(value)]

    return Critique(
        source=source,
        voter_model=voter_model,
        rating=rating,
        score=score,
        summary=str(payload.get("summary", "") or ""),
        issues=_as_str_list(payload.get("issues")),
        suggestions=_as_str_list(payload.get("suggestions")),
        details={
            k: v
            for k, v in payload.items()
            if k not in {"rating", "verdict", "score", "summary", "issues", "suggestions"}
        },
        timestamp=time.time(),
    )
