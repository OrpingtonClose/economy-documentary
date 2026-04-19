"""Dataclasses for the unified artifact critique + QA record layer.

See :mod:`critique` for the architectural rationale.  This module defines:

* :class:`ArtifactCritiqueRecord` — one per pipeline artifact.
* :class:`Critique` — a single LLM critic's structured perspective.
* :class:`QaVerdict` — a single deterministic QA / evaluator verdict.
* :class:`EscalationRef` — reference to a canonical escalation action taken
  on this artifact, so follow-up escalations can see what was tried.

The types are deliberately plain dataclasses + :class:`typing.Literal`
enums so they can be imported anywhere without pulling in the model stack
(same rule as ``orchestrator.escalation_menu``).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional, get_args


# ---------------------------------------------------------------------------
# Artifact taxonomy
# ---------------------------------------------------------------------------

ArtifactType = Literal[
    "scenario",        # top-level script structure (scenes.json, visual_style.json)
    "scene",           # a single narrative scene
    "visual_concept",  # a content-analyst / visual-concepter brief
    "clip",            # a single rendered video clip (one visual phrase)
    "audio",           # a single TTS narration clip
    "assembly",        # scene assembly or final documentary
]

ARTIFACT_TYPES: tuple[str, ...] = tuple(get_args(ArtifactType))


# ---------------------------------------------------------------------------
# Verdict / rating taxonomies
# ---------------------------------------------------------------------------

# QA verdict levels, ordered from best to worst.  ``escalate`` sits between
# ``warn`` and ``fail``: a voter is unsure and wants human/LLM review, but
# the artifact is not definitively broken.
QaVerdictStatus = Literal["pass", "warn", "escalate", "fail"]

QA_VERDICTS: tuple[str, ...] = tuple(get_args(QaVerdictStatus))

_QA_SEVERITY: dict[str, int] = {v: i for i, v in enumerate(QA_VERDICTS)}

# Critic rating scale.  Aligned with the coherence / scenario evaluator
# vocabulary already used elsewhere in the repo, plus ``UNKNOWN`` for
# critiques that could not render a decision (e.g. API error).
CritiqueRating = Literal["EXCELLENT", "GOOD", "FAIR", "POOR", "UNKNOWN"]


def worst_status(statuses: list[QaVerdictStatus]) -> QaVerdictStatus:
    """Return the worst (most severe) status in ``statuses``.

    ``pass`` < ``warn`` < ``escalate`` < ``fail``.  Empty input returns
    ``"pass"`` so the default state of "no verdicts yet" is benign.
    """

    if not statuses:
        return "pass"
    return max(statuses, key=lambda s: _QA_SEVERITY[s])


def artifact_type_and_id(record: "ArtifactCritiqueRecord") -> tuple[str, str]:
    """Return ``(artifact_type, artifact_id)`` for a record.

    Helper so callers do not reach into dataclass fields directly; keeps
    this the one place the composite identity is defined.
    """

    return record.artifact_type, record.artifact_id


# ---------------------------------------------------------------------------
# Inner records
# ---------------------------------------------------------------------------

@dataclass
class Critique:
    """A single LLM critic's structured perspective on an artifact.

    One ``Critique`` per (critic, artifact, iteration).  Critic squads
    (``ParallelAgent`` of 3–5 critics) land in a later PR; this schema is
    designed so the pull-based supervisor / deployment planner can read
    critiques from any squad without needing to know which squad produced
    them.

    Fields
    ------
    source:
        Critic identifier, e.g. ``"scenario_critic"``, ``"visual_critic"``,
        ``"brand_voice_critic"``, ``"timing_critic"``.  Free-form string
        so new critics can be added without schema churn.
    voter_model:
        Concrete model identifier passed to the provider SDK
        (e.g. ``"gemini-2.5-flash"``).  Empty when not applicable.
    rating:
        Overall rating from :data:`CritiqueRating`.  ``UNKNOWN`` means the
        critic could not render a decision (API error, skipped).
    score:
        Optional numeric score on an arbitrary scale defined by the
        critic.  The aggregator in ``qa_jury`` already does bias-corrected
        median across numeric scores; this field carries the raw score
        for downstream consumers that want to do the same.
    summary:
        Short human-readable verdict (one sentence).  Read by the
        escalation agent via :func:`read_artifact_critique_history`.
    issues:
        List of specific concerns the critic raised.
    suggestions:
        List of concrete remediation ideas (e.g. a prompt delta).
    """

    source: str
    voter_model: str = ""
    rating: CritiqueRating = "UNKNOWN"
    score: Optional[float] = None
    summary: str = ""
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("Critique.source must be non-empty")
        if self.rating not in get_args(CritiqueRating):
            raise ValueError(
                f"Critique.rating must be one of {get_args(CritiqueRating)}, "
                f"got {self.rating!r}"
            )
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Critique":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data).__name__}")
        return cls(
            source=str(data.get("source", "")),
            voter_model=str(data.get("voter_model", "")),
            rating=data.get("rating", "UNKNOWN"),
            score=data.get("score"),
            summary=str(data.get("summary", "")),
            issues=list(data.get("issues", []) or []),
            suggestions=list(data.get("suggestions", []) or []),
            details=dict(data.get("details", {}) or {}),
            timestamp=float(data.get("timestamp", 0.0) or 0.0),
        )


@dataclass
class QaVerdict:
    """A single deterministic QA / evaluator verdict on an artifact.

    Unlike :class:`Critique`, a ``QaVerdict`` represents the output of a
    hard gate (``gatekeeper``, ``qa_jury``, ``timeline_guardian``, the
    scenario and coherence evaluators) rather than an LLM critic's free
    opinion.  The two shapes are kept separate so the escalation agent
    can reason about "all hard gates passed but critics are split" vs.
    "structural invariants violated" without conflating them.

    Fields
    ------
    source:
        Gate identifier — e.g. ``"qa_jury"``, ``"gatekeeper"``,
        ``"timeline_guardian"``, ``"scenario_evaluator"``,
        ``"coherence_evaluator"``.
    check_name:
        Specific check within the source, e.g. ``"duration_match"`` for
        ``gatekeeper`` or ``"pronunciation"`` for ``qa_jury``.
    verdict:
        One of :data:`QA_VERDICTS`.
    confidence:
        For aggregated jury verdicts this is the fraction agreeing with
        the majority; otherwise ``1.0``.
    rating:
        Optional rating string from evaluators that produce categorical
        outcomes (``"EXCELLENT"``/``"GOOD"``/``"FAIR"``/``"POOR"``).
    message:
        Human-readable explanation.
    """

    source: str
    check_name: str
    verdict: QaVerdictStatus
    confidence: float = 1.0
    rating: Optional[str] = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("QaVerdict.source must be non-empty")
        if not self.check_name:
            raise ValueError("QaVerdict.check_name must be non-empty")
        if self.verdict not in QA_VERDICTS:
            raise ValueError(
                f"QaVerdict.verdict must be one of {QA_VERDICTS}, "
                f"got {self.verdict!r}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"QaVerdict.confidence must be in [0.0, 1.0], got {self.confidence}"
            )
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QaVerdict":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data).__name__}")
        return cls(
            source=str(data.get("source", "")),
            check_name=str(data.get("check_name", "")),
            verdict=data.get("verdict", "pass"),
            confidence=float(data.get("confidence", 1.0) or 0.0),
            rating=data.get("rating"),
            message=str(data.get("message", "")),
            details=dict(data.get("details", {}) or {}),
            timestamp=float(data.get("timestamp", 0.0) or 0.0),
        )


@dataclass
class EscalationRef:
    """Reference to a canonical escalation action taken on this artifact.

    Only the minimum needed for the escalation agent to recognise "we
    already tried X on this artifact and it failed" — the full
    :class:`orchestrator.escalation_menu.EscalationAction` is recorded
    elsewhere.

    Fields
    ------
    scope_id:
        ID of the :class:`orchestrator.escalation_scope.EscalationScope`
        that produced this action.
    action:
        Canonical action name (must match
        :data:`orchestrator.escalation_menu.ACTION_NAMES`).  Not validated
        here to keep the module dependency-free; callers that care should
        validate at construction.
    outcome:
        ``"success"`` / ``"failure"`` / ``"unknown"`` (default ``unknown``
        because the outcome is often learned after the fact).
    reasoning:
        The LLM's one-line justification, if any.
    """

    scope_id: str
    action: str
    outcome: Literal["success", "failure", "unknown"] = "unknown"
    reasoning: str = ""
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not self.scope_id:
            raise ValueError("EscalationRef.scope_id must be non-empty")
        if not self.action:
            raise ValueError("EscalationRef.action must be non-empty")
        if self.outcome not in ("success", "failure", "unknown"):
            raise ValueError(
                f"EscalationRef.outcome must be success/failure/unknown, "
                f"got {self.outcome!r}"
            )
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EscalationRef":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data).__name__}")
        return cls(
            scope_id=str(data.get("scope_id", "")),
            action=str(data.get("action", "")),
            outcome=data.get("outcome", "unknown"),
            reasoning=str(data.get("reasoning", "")),
            timestamp=float(data.get("timestamp", 0.0) or 0.0),
        )


# ---------------------------------------------------------------------------
# Top-level record
# ---------------------------------------------------------------------------

@dataclass
class ArtifactCritiqueRecord:
    """Unified critique + QA record for a single pipeline artifact.

    One record per ``(artifact_type, artifact_id)``.  Multiple generation
    attempts for the same logical artifact bump :attr:`iteration` rather
    than creating separate records — the escalation agent wants to see
    the full attempt history.

    ``critiques``, ``qa_results`` and ``escalations`` accumulate append-
    only: the store's append-* helpers read-modify-write rather than
    replace, so multiple agents writing concurrently do not clobber each
    other.

    The record is intentionally JSON-serialisable (``to_dict`` /
    ``from_dict``); the store persists it to disk and mirrors it to B2.
    """

    artifact_type: ArtifactType
    artifact_id: str
    iteration: int = 0
    produced_by: str = ""
    critiques: list[Critique] = field(default_factory=list)
    qa_results: list[QaVerdict] = field(default_factory=list)
    escalations: list[EscalationRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.artifact_type not in ARTIFACT_TYPES:
            raise ValueError(
                f"ArtifactCritiqueRecord.artifact_type must be one of "
                f"{ARTIFACT_TYPES}, got {self.artifact_type!r}"
            )
        if not self.artifact_id:
            raise ValueError("ArtifactCritiqueRecord.artifact_id must be non-empty")
        if self.iteration < 0:
            raise ValueError(
                f"ArtifactCritiqueRecord.iteration must be >= 0, got {self.iteration}"
            )
        now = time.time()
        if self.created_at == 0.0:
            self.created_at = now
        if self.updated_at == 0.0:
            self.updated_at = now

    # ------------------------------------------------------------------
    # Aggregate views
    # ------------------------------------------------------------------

    def worst_qa(self) -> QaVerdictStatus:
        """Return the worst QA verdict currently on the record."""

        return worst_status([q.verdict for q in self.qa_results])

    def latest_escalation(self) -> Optional[EscalationRef]:
        """Return the most recent escalation action, if any."""

        if not self.escalations:
            return None
        return max(self.escalations, key=lambda e: e.timestamp)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "artifact_id": self.artifact_id,
            "iteration": self.iteration,
            "produced_by": self.produced_by,
            "critiques": [c.to_dict() for c in self.critiques],
            "qa_results": [q.to_dict() for q in self.qa_results],
            "escalations": [e.to_dict() for e in self.escalations],
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactCritiqueRecord":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data).__name__}")
        return cls(
            artifact_type=data.get("artifact_type", "scene"),
            artifact_id=str(data.get("artifact_id", "")),
            iteration=int(data.get("iteration", 0) or 0),
            produced_by=str(data.get("produced_by", "")),
            critiques=[Critique.from_dict(c) for c in data.get("critiques", []) or []],
            qa_results=[QaVerdict.from_dict(q) for q in data.get("qa_results", []) or []],
            escalations=[
                EscalationRef.from_dict(e) for e in data.get("escalations", []) or []
            ],
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
        )
