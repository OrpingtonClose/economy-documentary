"""EscalationScope — the pull-based counterpart to ``EscalationContext``.

The legacy :class:`orchestrator.escalation_menu.EscalationContext` packs
every diagnostic the supervisor might want into a single dict before
calling ``supervisor_escalate``.  That shape is push-based: whoever
raises the escalation is responsible for gathering context the supervisor
*might* need, even when most of it is irrelevant to the decision.

PR-1 introduces :class:`EscalationScope` — a minimal descriptor the
supervisor/deployment-planner agent receives, plus ``scope_tags`` hinting
at which read-tools are worth consulting.  The agent then calls the
read-tools in :mod:`orchestrator.escalation_tools` to pull the data it
actually needs at decision time.

PR-1 does not change the supervisor wiring itself; it only lands the
scope dataclass so the read-tools (same PR) and the agent rewrite
(PR-2) can target a stable shape.  The legacy ``EscalationContext``
continues to work unchanged — both can coexist.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional, get_args

from critique.record import ARTIFACT_TYPES, ArtifactType


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------

FailureKind = Literal[
    "qa_fail",              # a hard QA gate rejected the artifact
    "critic_reject",        # one or more critic-squad LLMs rejected the artifact
    "worker_degraded",      # infra_agent flagged a GPU worker as degraded / unreachable
    "stage_timeout",        # infra_agent watchdog tripped (2x/4x expected duration)
    "cost_exceeded",        # budget / cost tracking exceeded a threshold
    "structural_invariant", # timeline / OTIO invariant violated
    "timing_violation",     # duration mismatch (video vs narration, gap, overrun)
    "production_fail",      # video / TTS generation crashed or returned an error
    "unknown",              # caller could not classify; agent decides
]

FAILURE_KINDS: tuple[str, ...] = tuple(get_args(FailureKind))


# Canonical tags the agent can use to decide which read-tools to call.
# This list is not a whitelist — callers can add free-form tags — but
# keeping the common ones here gives PR-2's supervisor prompt a stable
# vocabulary to reference.
KNOWN_SCOPE_TAGS: frozenset[str] = frozenset({
    "gpu_worker_degraded",
    "vram_pressure",
    "duration_violation",
    "narration_too_long",
    "narration_too_short",
    "anti_cheat_trigger",
    "adhd_compliance",
    "visual_coherence",
    "brand_voice",
    "cost_overrun",
    "stage_slow",
    "stage_hung",
    "jury_split",
    "critic_rejects_majority",
    "otio_structural",
})


# ---------------------------------------------------------------------------
# EscalationScope
# ---------------------------------------------------------------------------

class EscalationScopeError(ValueError):
    """Raised when an :class:`EscalationScope` fails validation."""


@dataclass
class EscalationScope:
    """Minimal descriptor of "what happened" + pointers for the agent.

    The agent receives one :class:`EscalationScope` and a toolbox of
    read-only lookup functions (see :mod:`orchestrator.escalation_tools`).
    Everything else — artifact critique history, worker health, stage
    timing, vast.ai cost, prior escalations — is pulled via tool calls.

    Fields
    ------
    scope_id:
        Unique identifier for this escalation event.  Referenced by
        :class:`critique.record.EscalationRef.scope_id` once an action is
        decided, so later escalations can see the chain.
    failure_kind:
        Classification from :data:`FailureKind`.  Agents should treat
        ``"unknown"`` as "I need to read the critique history before I
        can tell you what's going on".
    trigger_message:
        Short human-readable sentence describing the failure.  Copied
        verbatim into the supervisor prompt; keep it under ~200 chars.
    stage_name:
        Pipeline stage the failure originated in (``"scenario"`` /
        ``"audio"`` / ``"visual_direction"`` / ``"production"`` /
        ``"assembly"`` / ``"gatekeeper"``).
    primary_artifact_id / primary_artifact_type:
        Pointer to the artifact that "broke", if applicable.  May be
        ``None`` for infra-level scopes (``worker_degraded``,
        ``stage_timeout``, ``cost_exceeded``).
    scope_tags:
        Free-form hints at which read-tools to consult.  See
        :data:`KNOWN_SCOPE_TAGS` for the canonical set.
    summary_counters:
        Cheap pre-computed numbers so trivial scopes resolve without
        extra tool calls (e.g. ``{"regen_count": 3, "qa_fail_streak": 2,
        "consecutive_worker_failures": 4}``).
    high_cost:
        Hint to use Pro instead of Flash for the supervisor model — same
        semantics as :class:`orchestrator.escalation_menu.EscalationContext.high_cost`.
    related_artifacts:
        Optional list of ``(artifact_type, artifact_id)`` pairs beyond
        the primary artifact that the agent may want to inspect.
    """

    failure_kind: FailureKind
    trigger_message: str
    stage_name: str = ""
    scope_id: str = ""
    primary_artifact_id: Optional[str] = None
    primary_artifact_type: Optional[ArtifactType] = None
    scope_tags: list[str] = field(default_factory=list)
    summary_counters: dict[str, int] = field(default_factory=dict)
    high_cost: bool = False
    related_artifacts: list[tuple[str, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        if self.failure_kind not in FAILURE_KINDS:
            raise EscalationScopeError(
                f"failure_kind must be one of {FAILURE_KINDS}, "
                f"got {self.failure_kind!r}"
            )
        if not self.trigger_message:
            raise EscalationScopeError("trigger_message must be non-empty")
        if self.primary_artifact_type is not None and self.primary_artifact_type not in ARTIFACT_TYPES:
            raise EscalationScopeError(
                f"primary_artifact_type must be one of {ARTIFACT_TYPES}, "
                f"got {self.primary_artifact_type!r}"
            )
        # Normalise: if artifact_id is present but type isn't, require type.
        if self.primary_artifact_id and not self.primary_artifact_type:
            raise EscalationScopeError(
                "primary_artifact_id requires primary_artifact_type"
            )
        # Deduplicate tags; strip whitespace; drop empty strings.
        cleaned_tags: list[str] = []
        seen: set[str] = set()
        for tag in self.scope_tags:
            t = str(tag).strip()
            if not t or t in seen:
                continue
            seen.add(t)
            cleaned_tags.append(t)
        self.scope_tags = cleaned_tags
        # Validate summary_counters values.
        for k, v in self.summary_counters.items():
            if not isinstance(v, int) or isinstance(v, bool):
                raise EscalationScopeError(
                    f"summary_counters[{k!r}] must be int, got {type(v).__name__}"
                )
        # Normalise related_artifacts into (type, id) tuples.
        normalised: list[tuple[str, str]] = []
        for pair in self.related_artifacts:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise EscalationScopeError(
                    f"related_artifacts entries must be (type, id) pairs, got {pair!r}"
                )
            at, aid = str(pair[0]), str(pair[1])
            if at not in ARTIFACT_TYPES:
                raise EscalationScopeError(
                    f"related_artifacts type {at!r} not in {ARTIFACT_TYPES}"
                )
            if not aid:
                raise EscalationScopeError("related_artifacts id must be non-empty")
            normalised.append((at, aid))
        self.related_artifacts = normalised
        if not self.scope_id:
            self.scope_id = f"esc_{uuid.uuid4().hex[:12]}"
        if self.created_at == 0.0:
            self.created_at = time.time()

    # ------------------------------------------------------------------
    # Prompt presentation
    # ------------------------------------------------------------------

    def to_prompt(self) -> str:
        """Render the scope as a compact block for the supervisor prompt.

        Kept short on purpose: the agent is expected to call read-tools
        for anything richer than what this returns.  The output is
        stable / human-readable so prompt diffs stay reviewable.
        """

        lines: list[str] = [
            "ESCALATION SCOPE",
            f"  scope_id       : {self.scope_id}",
            f"  failure_kind   : {self.failure_kind}",
            f"  stage          : {self.stage_name or '-'}",
            f"  trigger        : {self.trigger_message}",
        ]
        if self.primary_artifact_id:
            lines.append(
                f"  primary        : {self.primary_artifact_type}:{self.primary_artifact_id}"
            )
        if self.related_artifacts:
            pretty = ", ".join(f"{t}:{i}" for t, i in self.related_artifacts)
            lines.append(f"  related        : {pretty}")
        if self.scope_tags:
            lines.append(f"  tags           : {', '.join(self.scope_tags)}")
        if self.summary_counters:
            pretty = ", ".join(f"{k}={v}" for k, v in sorted(self.summary_counters.items()))
            lines.append(f"  counters       : {pretty}")
        if self.high_cost:
            lines.append("  high_cost      : true  (use Pro-tier supervisor model)")
        lines.append("")
        lines.append(
            "Use the escalation read-tools to pull artifact critiques, QA verdicts, "
            "worker health, stage timing, cost, and prior escalations as needed."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # related_artifacts are tuples; asdict serialises them fine but
        # readers expect a list-of-lists in JSON.
        data["related_artifacts"] = [list(p) for p in self.related_artifacts]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EscalationScope":
        if not isinstance(data, dict):
            raise EscalationScopeError(
                f"Expected dict, got {type(data).__name__}"
            )
        related_raw = data.get("related_artifacts") or []
        related = [tuple(pair) for pair in related_raw]
        return cls(
            failure_kind=data.get("failure_kind", "unknown"),
            trigger_message=str(data.get("trigger_message", "")),
            stage_name=str(data.get("stage_name", "")),
            scope_id=str(data.get("scope_id", "")),
            primary_artifact_id=data.get("primary_artifact_id"),
            primary_artifact_type=data.get("primary_artifact_type"),
            scope_tags=list(data.get("scope_tags", []) or []),
            summary_counters=dict(data.get("summary_counters", {}) or {}),
            high_cost=bool(data.get("high_cost", False)),
            related_artifacts=related,
            metadata=dict(data.get("metadata", {}) or {}),
            created_at=float(data.get("created_at", 0.0) or 0.0),
        )


__all__ = [
    "FAILURE_KINDS",
    "FailureKind",
    "KNOWN_SCOPE_TAGS",
    "EscalationScope",
    "EscalationScopeError",
]
