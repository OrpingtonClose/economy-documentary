"""ARCH-C3 — Failure orchestrator (routes every failure into the right ladder).

Closes #142 under parent #125 / meta #122.  Wires together:

    * ARCH-C1 :mod:`agents.diagnostic_classifier`  (classifier, #140 / PR #163)
    * ARCH-C2 :mod:`infra_ladder`                   (infra ladder, #141 / PR #168)
    * existing :mod:`recovery` + creative menu     (content ladder, #61/#102/#103)
    * same human gate as both                      (``recovery.submit_escalation``)

Architecture (Diagram 8 of ``docs/ARCHITECTURE.md``)::

    ┌──────────────────────────────────────────────────────────────┐
    │                     FailureOrchestrator                       │
    │                                                                │
    │   ┌─────────────────────────────────────────────────────┐    │
    │   │  1. diagnostic_classifier.classify_failure(event)    │    │
    │   └─────────────────────────────────────────────────────┘    │
    │                            │                                   │
    │        ┌───────────────────┼───────────────────┐              │
    │        ▼                   ▼                   ▼              │
    │    content              infra              unclear             │
    │        │                   │                   │              │
    │        ▼                   ▼                   ▼              │
    │  supervisor_          run_infra_         diagnostic-loop       │
    │  escalate (creative   ladder (L0-L3 ops  (enrich telemetry     │
    │  L1-L3 menu)          recovery)          + reclassify; at most │
    │        │                   │             ``max_rounds`` tries) │
    │        │                   │                   │              │
    │        └─────────┬─────────┴─────────┬─────────┘              │
    │                  ▼                    ▼                        │
    │            resolved (return action)   unresolved                │
    │                                        │                        │
    │                                        ▼                        │
    │                             L4 human gate                       │
    │                     recovery.submit_escalation()               │
    └──────────────────────────────────────────────────────────────┘

Fail-loud invariants (enforced by :func:`_assert_no_silent_downgrade` in
the ``after_agent_callback``):

    * The orchestrator never mangles one ladder's actions into the other's.
      ``recovery._CANONICAL_TO_CALLER`` (creative-only by design, see
      c697525 on PR #117) is deliberately NOT consulted here.  The push
      path only ever surfaces CREATIVE actions to the supervisor.
    * A ``content`` route may not come back with an :class:`InfraRecoveryAction`.
    * An ``infra`` route may not come back with an :class:`EscalationAction`.
    * L4 terminal requires ``escalation_id`` populated — the L4 gate is
      the SAME one used by :func:`recovery.submit_escalation` and by the
      infra ladder's :func:`infra_ladder.infra_l4_human`.
    * An ``unclear`` classification that survives ``max_rounds`` of the
      diagnostic loop goes to L4 — never silently retried forever, never
      silently routed to a default ladder.

ADK surface (built lazily — ``google-adk`` is optional in minimal test envs):

    * ``FailureOrchestratorAgent`` — a ``BaseAgent`` subclass that reads
      ``state["failure_event"]`` and (optionally) ``state["infra_telemetry"]``
      from the blackboard, runs the whole pipeline, and writes back:

          state["failure_orchestrator_result"]    # structured decision dict
          state["failure_orchestrator_summary"]   # human-readable one-liner

      Written with ``output_key``-style semantics to match the Scenario
      Director / Visual Director pattern.
    * An ``after_agent_callback`` performs the boundary invariant checks
      above (Timeline-Guardian pattern).

Usage (outside ADK — most existing call sites)::

    from failure_orchestrator import (
        FailureEvent, InfraTelemetry, route_failure,
    )

    decision = route_failure(event, telemetry)
    if decision.route == "content_ladder":
        # caller hands decision.creative_action back to the creative exec path
        ...
    elif decision.route == "infra_ladder":
        # caller hands decision.infra_action back to the infra executor
        ...
    else:  # "human_escalation"
        # caller awaits the dashboard response via recovery.submit_escalation
        ...
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from agents.diagnostic_classifier import (
    CLASSIFICATION_CONTENT,
    CLASSIFICATION_INFRA,
    CLASSIFICATION_UNCLEAR,
    Classification,
    FailureEvent,
    InfraTelemetry,
    classify_failure,
)
from infra_ladder import (
    KNOWN_INFRA_SIGNATURES,
    InfraFailureEvent,
    InfraLadderResult,
    run_infra_ladder,
)
from orchestrator.escalation_menu import (
    ACTION_NAMES,
    EscalationAction,
    EscalationContext,
)
from recovery import (
    HumanEscalationRequest,
    RecoveryLevel,
    _next_escalation_id,
    submit_escalation,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants (blackboard keys, routes, diagnostic-loop defaults)
# ---------------------------------------------------------------------------

# Input keys (shared with the classifier ADK agent — same contract).
BLACKBOARD_FAILURE_EVENT_KEY = "failure_event"
BLACKBOARD_INFRA_TELEMETRY_KEY = "infra_telemetry"

# Output keys.
BLACKBOARD_RESULT_KEY = "failure_orchestrator_result"
"""Structured :class:`OrchestratorDecision` dict written by the orchestrator."""

BLACKBOARD_SUMMARY_KEY = "failure_orchestrator_summary"
"""Short human-readable summary, mirrors the Scenario Director ``output_key``."""

BLACKBOARD_DIAGNOSTIC_TRAIL_KEY = "failure_orchestrator_diagnostic_trail"
"""Ordered list of diagnostic loop attempts (each a Classification dict)."""

# Route values — deterministic short strings.
ROUTE_CONTENT_LADDER = "content_ladder"
ROUTE_INFRA_LADDER = "infra_ladder"
ROUTE_HUMAN_ESCALATION = "human_escalation"

VALID_ROUTES = frozenset(
    {ROUTE_CONTENT_LADDER, ROUTE_INFRA_LADDER, ROUTE_HUMAN_ESCALATION}
)

# Resolutions.
RESOLUTION_CONTENT = "content"
RESOLUTION_INFRA = "infra"
RESOLUTION_UNCLEAR_TIMEOUT = "unclear_timeout"

# Diagnostic loop cap.  Small by construction — the loop is meant to
# pick up a piece of telemetry that was missing the first time, not to
# wait-loop for a condition to change.  After ``MAX_DIAGNOSTIC_ROUNDS``
# we escalate to L4 rather than silently spinning.
MAX_DIAGNOSTIC_ROUNDS = 2


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FailureOrchestratorError(RuntimeError):
    """Fail-loud error for orchestrator contract violations.

    Raised (never swallowed) when:
        * A dep returns an action of the wrong kind for the chosen route.
        * A terminal L4 decision is missing an ``escalation_id``.
        * Required blackboard inputs are missing.
    """


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorDecision:
    """Terminal decision emitted for a single failure.

    Exactly one of ``creative_action`` / ``infra_action`` / ``escalation_id``
    will be populated, chosen by ``route``.  This invariant is validated
    by the ADK ``after_agent_callback`` — a mismatched shape is a
    fail-loud error, never a silent downgrade.
    """

    resolution: str
    """One of :data:`RESOLUTION_CONTENT`, :data:`RESOLUTION_INFRA`,
    :data:`RESOLUTION_UNCLEAR_TIMEOUT`.  Captures what the classifier
    ultimately returned (after any diagnostic loop)."""

    route: str
    """One of :data:`VALID_ROUTES`.  The ladder the orchestrator dispatched
    the failure to."""

    classification: dict
    """Final :meth:`Classification.to_dict` result that drove the route."""

    creative_action: Optional[dict] = None
    """If ``route == ROUTE_CONTENT_LADDER``, the creative
    :class:`EscalationAction` the supervisor chose (as a dict)."""

    infra_action: Optional[dict] = None
    """If ``route == ROUTE_INFRA_LADDER``, the :class:`InfraRecoveryAction`
    the infra ladder chose (as a dict)."""

    infra_terminal_level: Optional[int] = None
    """If the infra ladder ran, the level that terminated it (L0..L4)."""

    escalation_id: Optional[str] = None
    """Set when a ladder terminated at L4 and handed off to the human
    dashboard gate, OR when the diagnostic loop timed out."""

    diagnostic_trail: list[dict] = field(default_factory=list)
    """Ordered list of classifications returned during the unclear-loop
    (including the final one).  Empty on the first-pass-classified paths."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution,
            "route": self.route,
            "classification": dict(self.classification),
            "creative_action": (
                dict(self.creative_action) if self.creative_action else None
            ),
            "infra_action": (
                dict(self.infra_action) if self.infra_action else None
            ),
            "infra_terminal_level": self.infra_terminal_level,
            "escalation_id": self.escalation_id,
            "diagnostic_trail": [dict(x) for x in self.diagnostic_trail],
        }


# ---------------------------------------------------------------------------
# Dependency wiring
# ---------------------------------------------------------------------------


ClassifyCallable = Callable[
    [FailureEvent, Optional[InfraTelemetry], bool, Optional[dict]],
    Classification,
]
"""``(event, telemetry, use_llm, state) -> Classification``."""

EnrichTelemetryCallable = Callable[
    [FailureEvent, Optional[InfraTelemetry]],
    Optional[InfraTelemetry],
]
"""Invoked in the diagnostic loop to pull a fresh telemetry snapshot."""

RunInfraLadderCallable = Callable[[InfraFailureEvent], InfraLadderResult]

SupervisorEscalateCallable = Callable[[EscalationContext], EscalationAction]

SubmitEscalationCallable = Callable[[HumanEscalationRequest], None]

NextEscalationIdCallable = Callable[[], str]


@dataclass
class FailureOrchestratorDeps:
    """Injectable callables for the orchestrator.

    All default to the live wiring.  Tests inject fakes to avoid touching
    network, the LLM, or the disk.
    """

    classify: ClassifyCallable
    enrich_telemetry: EnrichTelemetryCallable
    run_infra_ladder: RunInfraLadderCallable
    supervisor_escalate: SupervisorEscalateCallable
    submit_human_escalation: SubmitEscalationCallable
    next_escalation_id: NextEscalationIdCallable


# -- Default dep implementations -------------------------------------------


def _default_classify(
    event: FailureEvent,
    telemetry: Optional[InfraTelemetry],
    use_llm: bool,
    state: Optional[dict],
) -> Classification:
    return classify_failure(
        event,
        telemetry,
        use_llm=use_llm,
        state=state,
    )


def _default_enrich_telemetry(
    event: FailureEvent,
    existing: Optional[InfraTelemetry],
) -> Optional[InfraTelemetry]:
    """Pull a fresh :class:`InfraTelemetry` snapshot from the live ``InfraAgent``.

    This is the one tool the diagnostic loop has that a plain classifier
    call does not — between loop iterations we re-query the infra agent
    to see whether worker-health signals have become unambiguous.

    Returns the existing telemetry (unchanged) if the live agent is not
    reachable.  We deliberately do NOT synthesise telemetry out of thin
    air: if the agent isn't there, we let the classifier see the same
    inputs a second time and fall through to L4 on unclear timeout.
    """
    try:
        from infra_agent import get_infra_agent

        agent = get_infra_agent()
        if agent is None:
            return existing
        if not event.worker_id:
            return existing
        snapshot = agent.get_worker_snapshot(event.worker_id)
        if snapshot is None:
            return existing
        return InfraTelemetry(
            worker_status=str(snapshot.get("status") or (
                existing.worker_status if existing else ""
            )),
            worker_last_error=str(snapshot.get("last_error") or (
                existing.worker_last_error if existing else ""
            )),
            consecutive_failures=int(snapshot.get("consecutive_failures") or (
                existing.consecutive_failures if existing else 0
            )),
            systemic_patterns=list(snapshot.get("systemic_patterns") or (
                existing.systemic_patterns if existing else []
            )),
            vm_escalation_severity=str(snapshot.get("vm_escalation_severity") or (
                existing.vm_escalation_severity if existing else ""
            )),
            model_loaded=(
                snapshot["model_loaded"]
                if "model_loaded" in snapshot
                else (existing.model_loaded if existing else None)
            ),
        )
    except Exception as exc:
        logger.warning(
            "FailureOrchestrator: live telemetry enrichment failed (%s); "
            "keeping existing telemetry",
            exc,
        )
        return existing


def _default_supervisor_escalate(context: EscalationContext) -> EscalationAction:
    # Imported lazily — the supervisor module depends on ADK and LLM stacks.
    from agents.production_supervisor import supervisor_escalate

    return supervisor_escalate(context)


def _default_submit_escalation(req: HumanEscalationRequest) -> None:
    submit_escalation(req)


def _default_next_escalation_id() -> str:
    return _next_escalation_id()


def build_default_deps() -> FailureOrchestratorDeps:
    return FailureOrchestratorDeps(
        classify=_default_classify,
        enrich_telemetry=_default_enrich_telemetry,
        run_infra_ladder=run_infra_ladder,
        supervisor_escalate=_default_supervisor_escalate,
        submit_human_escalation=_default_submit_escalation,
        next_escalation_id=_default_next_escalation_id,
    )


# ---------------------------------------------------------------------------
# Helpers: build infra / creative contexts from a generic FailureEvent
# ---------------------------------------------------------------------------


def _infer_infra_signature(event: FailureEvent) -> str:
    """Best-effort mapping from the failure text to one of
    :data:`infra_ladder.KNOWN_INFRA_SIGNATURES`.

    The classifier already proved the failure is infra; here we just have
    to pick the most specific signature so the infra ladder can apply
    targeted recovery (e.g. OOM → hot-swap tier).  On no-match we use
    ``worker_death`` which is the catch-all the ladder handles correctly.
    """
    haystack = " ".join(
        filter(
            None,
            (event.error_message, event.exception_type, event.stack_trace),
        )
    ).lower()

    # Order matters — most specific first.
    patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("oom", ("oom", "out of memory", "cuda out of memory", "memory error")),
        ("vram_exhausted", ("vram", "gpu memory full", "out of gpu memory")),
        ("cuda_error", ("cuda error", "cuda_error", "cudnn", "nccl")),
        ("driver_reset", ("driver reset", "gpu driver", "nvidia-smi", "nvml")),
        ("thermal_throttle", ("thermal", "overheating", "throttle")),
        ("preemption", ("preempt", "spot interrupt", "instance terminated")),
        ("cold_start_fail", ("cold start", "boot failed", "failed to boot")),
        # storage_unreachable comes before network_partition so the more
        # specific storage error paths win (both can contain the word
        # "unreachable").
        ("storage_unreachable", ("s3", "b2", "bucket", "storage unreachable")),
        (
            "network_partition",
            ("connection refused", "timed out", "timeout", "unreachable",
             "econnrefused", "enetunreach", "network is unreachable"),
        ),
        ("auth_revoked", ("401", "unauthorized", "forbidden", "auth revoked")),
        ("billing_trip", ("billing", "quota", "rate limit", "429")),
        ("provider_outage", ("provider outage", "vast.ai", "5xx", "500 internal")),
        ("worker_death", ("process crashed", "worker died", "killed", "sigkill",
                          "segmentation fault")),
    )
    for name, needles in patterns:
        if any(n in haystack for n in needles):
            if name in KNOWN_INFRA_SIGNATURES:
                return name
    return "worker_death"


def _build_infra_event(
    event: FailureEvent,
    classification: Classification,
) -> InfraFailureEvent:
    """Translate a generic :class:`FailureEvent` into an :class:`InfraFailureEvent`.

    Fail-loud: if the caller didn't provide a ``worker_id`` we still pass
    the empty string through — the infra ladder will see ``worker_url=""``
    and escalate L0 correctly (no healthy worker to retry on).  We never
    invent a worker URL.
    """
    return InfraFailureEvent(
        job_id=event.operation_name,
        worker_url=event.worker_id,
        failure_signature=_infer_infra_signature(event),
        raw_error=event.error_message,
        classification=classification.to_dict(),
        metadata={"pipeline_stage": event.pipeline_stage},
    )


def _build_escalation_context(
    event: FailureEvent,
    classification: Classification,
) -> EscalationContext:
    """Build the :class:`EscalationContext` consulted by the creative
    supervisor for a ``content``-classified failure.

    NOTE: by construction, this context never includes any infra
    recovery action names.  The supervisor only ever sees
    :data:`orchestrator.escalation_menu.ACTION_NAMES` — the five
    creative actions (c697525 on PR #117).  That is the push-path
    invariant this orchestrator preserves.
    """
    qa = event.qa_reason or event.error_message
    return EscalationContext(
        failing_artifact=event.operation_name,
        artifact_descriptor={
            "pipeline_stage": event.pipeline_stage,
            "exception_type": event.exception_type,
            "qa_verdict": event.qa_verdict,
            "qa_reason": qa[:500],
        },
        timeline_state_snapshot={},
        user_original_prompt="",
        budget_remaining=0.0,
        escalation_history=[
            {
                "diagnostic_classification": classification.to_dict(),
            }
        ],
        high_cost=False,
    )


# ---------------------------------------------------------------------------
# Route dispatchers — one per ladder
# ---------------------------------------------------------------------------


def _dispatch_content(
    event: FailureEvent,
    classification: Classification,
    deps: FailureOrchestratorDeps,
    diagnostic_trail: list[Classification],
) -> OrchestratorDecision:
    """Run the existing creative escalation ladder.

    We do NOT re-enter :func:`recovery.escalate_pipeline_error` here: that
    function runs the Python-side L0–L3 agents, which are gated behind
    the heavyweight pipeline state.  ARCH-C3's job is to pick the
    canonical creative action — the supervisor is the canonical
    decision-maker for the creative ladder (#102).  The caller then
    executes the action against the OTIO timeline.

    Fail-loud: if the supervisor ever returns a non-creative action, we
    raise.  ``_CANONICAL_TO_CALLER`` is NEVER consulted on this path — it
    is a pull-path shim for the legacy caller vocabulary, and using it
    here would silently downgrade ``abort_run`` into ``"abort"`` and lose
    the canonical action name (c697525 rationale).
    """
    context = _build_escalation_context(event, classification)
    action = deps.supervisor_escalate(context)

    if not isinstance(action, EscalationAction):
        raise FailureOrchestratorError(
            "Content dispatch: supervisor_escalate must return an "
            f"EscalationAction, got {type(action).__name__!r}"
        )
    if action.action not in ACTION_NAMES:
        raise FailureOrchestratorError(
            f"Content dispatch: supervisor returned non-creative action "
            f"{action.action!r}; refusing to silently downgrade "
            f"(push path only accepts {sorted(ACTION_NAMES)})."
        )

    if action.action == "abort_run":
        # abort_run is a creative terminal action — it is handled by the
        # creative ladder, not by the human gate.  We still surface route
        # as content_ladder: the caller aborts the run based on
        # creative_action.  No silent downgrade, no human escalation.
        return OrchestratorDecision(
            resolution=RESOLUTION_CONTENT,
            route=ROUTE_CONTENT_LADDER,
            classification=classification.to_dict(),
            creative_action=asdict(action),
            diagnostic_trail=[c.to_dict() for c in diagnostic_trail],
        )

    return OrchestratorDecision(
        resolution=RESOLUTION_CONTENT,
        route=ROUTE_CONTENT_LADDER,
        classification=classification.to_dict(),
        creative_action=asdict(action),
        diagnostic_trail=[c.to_dict() for c in diagnostic_trail],
    )


def _dispatch_infra(
    event: FailureEvent,
    classification: Classification,
    deps: FailureOrchestratorDeps,
    diagnostic_trail: list[Classification],
) -> OrchestratorDecision:
    """Run the ARCH-C2 infra ladder.

    Terminal outcomes:
        * success (L0..L3 resolved) → ``route = ROUTE_INFRA_LADDER``,
          ``infra_action`` populated.
        * L4 (ladder exhausted) → ``route = ROUTE_HUMAN_ESCALATION``,
          ``escalation_id`` populated.  The infra ladder already
          submitted to :func:`recovery.submit_escalation` inside
          :func:`infra_ladder.infra_l4_human` — same gate content L4 uses.
    """
    infra_event = _build_infra_event(event, classification)
    ladder_result = deps.run_infra_ladder(infra_event)

    if not isinstance(ladder_result, InfraLadderResult):
        raise FailureOrchestratorError(
            "Infra dispatch: run_infra_ladder must return InfraLadderResult, "
            f"got {type(ladder_result).__name__!r}"
        )

    action_dict = (
        ladder_result.action.to_dict() if ladder_result.action is not None else None
    )

    if ladder_result.success:
        return OrchestratorDecision(
            resolution=RESOLUTION_INFRA,
            route=ROUTE_INFRA_LADDER,
            classification=classification.to_dict(),
            infra_action=action_dict,
            infra_terminal_level=ladder_result.terminal_level,
            diagnostic_trail=[c.to_dict() for c in diagnostic_trail],
        )

    # Ladder exhausted — L4 human gate.  The infra ladder's L4 tool
    # already submitted the escalation (same gate as content L4); we
    # surface the escalation_id so the caller can await the dashboard
    # response.
    if ladder_result.terminal_level != int(RecoveryLevel.HUMAN):
        raise FailureOrchestratorError(
            "Infra dispatch: ladder returned success=False at "
            f"level={ladder_result.terminal_level}, expected L4 (HUMAN)"
        )
    if not ladder_result.escalation_id:
        raise FailureOrchestratorError(
            "Infra dispatch: L4 terminal but escalation_id is missing — "
            "ladder violated its own L4 contract; refusing to silently drop"
        )

    return OrchestratorDecision(
        resolution=RESOLUTION_INFRA,
        route=ROUTE_HUMAN_ESCALATION,
        classification=classification.to_dict(),
        infra_action=action_dict,
        infra_terminal_level=ladder_result.terminal_level,
        escalation_id=ladder_result.escalation_id,
        diagnostic_trail=[c.to_dict() for c in diagnostic_trail],
    )


def _escalate_unclear_to_human(
    event: FailureEvent,
    classification: Classification,
    diagnostic_trail: list[Classification],
    deps: FailureOrchestratorDeps,
) -> OrchestratorDecision:
    """Submit an ``unclear_timeout`` straight to the L4 dashboard gate.

    Same gate as both content and infra L4 (:func:`recovery.submit_escalation`).
    The caller is responsible for awaiting the dashboard response.
    """
    escalation_id = deps.next_escalation_id()
    diagnosis = {
        "root_cause": (
            f"Diagnostic classifier could not reach a confident "
            f"classification after {len(diagnostic_trail)} rounds for "
            f"{event.operation_name}."
        ),
        "confidence": "unresolved",
        "proposed_fix": (
            "Manual triage: determine whether the failure is content- or "
            "infra-rooted and route to the appropriate ladder manually."
        ),
        "raw_error": event.error_message[:500],
        "final_classification": classification.to_dict(),
        "diagnostic_trail": [c.to_dict() for c in diagnostic_trail],
    }
    req = HumanEscalationRequest(
        id=escalation_id,
        operation_name=f"failure_orchestrator:{event.operation_name}",
        error_chain=[],
        diagnosis=diagnosis,
        proposed_actions=[
            {
                "action_id": "classify_content",
                "description": "Force-route to the creative ladder",
                "risk_level": "medium",
            },
            {
                "action_id": "classify_infra",
                "description": "Force-route to the infra ladder",
                "risk_level": "medium",
            },
            {
                "action_id": "abort",
                "description": "Abort the pipeline run",
                "risk_level": "high",
            },
        ],
        severity="critical",
        timestamp=time.time(),
    )
    deps.submit_human_escalation(req)

    return OrchestratorDecision(
        resolution=RESOLUTION_UNCLEAR_TIMEOUT,
        route=ROUTE_HUMAN_ESCALATION,
        classification=classification.to_dict(),
        escalation_id=escalation_id,
        diagnostic_trail=[c.to_dict() for c in diagnostic_trail],
    )


# ---------------------------------------------------------------------------
# Public callable — the orchestrator failure entry point
# ---------------------------------------------------------------------------


def route_failure(
    event: FailureEvent,
    infra_telemetry: Optional[InfraTelemetry] = None,
    *,
    state: Optional[dict] = None,
    deps: Optional[FailureOrchestratorDeps] = None,
    max_diagnostic_rounds: int = MAX_DIAGNOSTIC_ROUNDS,
    use_llm: bool = True,
) -> OrchestratorDecision:
    """Classify one failure and dispatch it to the correct ladder.

    This is the single canonical entry point the production pipeline
    should call from its failure-catching layer.  It:

        1. Runs the diagnostic classifier.
        2. If ``content`` → dispatches to the creative supervisor.
        3. If ``infra`` → dispatches to the infra ladder.
        4. If ``unclear`` → enters a bounded diagnostic loop that
           re-queries live telemetry and re-classifies up to
           ``max_diagnostic_rounds`` times.  If still unclear, escalates
           to the L4 dashboard gate.

    The function is fail-loud on dep contract violations — the
    orchestrator never silently degrades or routes around the classifier.

    Parameters
    ----------
    event:
        The failure event.
    infra_telemetry:
        Optional snapshot.  May be ``None`` — the diagnostic loop will
        enrich it on the fly via :attr:`FailureOrchestratorDeps.enrich_telemetry`.
    state:
        Optional ADK-blackboard dict.  When supplied, the orchestrator
        writes its decision under :data:`BLACKBOARD_RESULT_KEY`, a
        one-liner under :data:`BLACKBOARD_SUMMARY_KEY`, and the
        diagnostic trail under :data:`BLACKBOARD_DIAGNOSTIC_TRAIL_KEY`.
    deps:
        Optional :class:`FailureOrchestratorDeps` override; defaults to
        :func:`build_default_deps`.
    max_diagnostic_rounds:
        Hard cap on unclear-loop iterations.  Must be ``>= 0``.  A value
        of ``0`` disables the loop — the first unclear classification
        goes straight to L4 (useful for tests).
    use_llm:
        Forwarded to the classifier.  Set to ``False`` in offline test
        environments.
    """
    if not isinstance(event, FailureEvent):
        raise TypeError(
            f"route_failure: event must be FailureEvent, got {type(event).__name__}"
        )
    if max_diagnostic_rounds < 0:
        raise ValueError(
            f"route_failure: max_diagnostic_rounds must be >= 0, got "
            f"{max_diagnostic_rounds}"
        )
    deps = deps or build_default_deps()
    telemetry = infra_telemetry
    diagnostic_trail: list[Classification] = []

    # Round 0 — first classification.
    classification = deps.classify(event, telemetry, use_llm, state)
    diagnostic_trail.append(classification)

    # Diagnostic loop — only entered if first pass is unclear.
    rounds_run = 0
    while (
        classification.classification == CLASSIFICATION_UNCLEAR
        and rounds_run < max_diagnostic_rounds
    ):
        rounds_run += 1
        enriched = deps.enrich_telemetry(event, telemetry)
        if enriched is telemetry and rounds_run >= max_diagnostic_rounds:
            # No new evidence and we've used the last slot — break and
            # let the outer code escalate.  This prevents spinning on
            # stale inputs.
            break
        telemetry = enriched
        logger.info(
            "FailureOrchestrator: unclear classification (round %d/%d); "
            "re-classifying with refreshed telemetry",
            rounds_run, max_diagnostic_rounds,
        )
        classification = deps.classify(event, telemetry, use_llm, state)
        diagnostic_trail.append(classification)

    # Dispatch.
    if classification.classification == CLASSIFICATION_CONTENT:
        decision = _dispatch_content(event, classification, deps, diagnostic_trail)
    elif classification.classification == CLASSIFICATION_INFRA:
        decision = _dispatch_infra(event, classification, deps, diagnostic_trail)
    else:
        # Unclear timeout — straight to L4.
        decision = _escalate_unclear_to_human(
            event, classification, diagnostic_trail, deps
        )

    _assert_no_silent_downgrade(decision)

    if state is not None:
        state[BLACKBOARD_RESULT_KEY] = decision.to_dict()
        state[BLACKBOARD_SUMMARY_KEY] = _summarise_decision(decision)
        state[BLACKBOARD_DIAGNOSTIC_TRAIL_KEY] = [
            c.to_dict() for c in diagnostic_trail
        ]

    logger.info(
        "FailureOrchestrator: op=%s resolution=%s route=%s "
        "creative=%s infra=%s escalation_id=%s rounds=%d",
        event.operation_name,
        decision.resolution,
        decision.route,
        decision.creative_action["action"] if decision.creative_action else None,
        decision.infra_action["action_type"] if decision.infra_action else None,
        decision.escalation_id,
        len(diagnostic_trail),
    )
    return decision


# ---------------------------------------------------------------------------
# Invariant check (push-path boundary guard)
# ---------------------------------------------------------------------------


def _assert_no_silent_downgrade(decision: OrchestratorDecision) -> None:
    """Fail-loud invariant check — mirrors the Timeline Guardian.

    The orchestrator must never silently re-label one ladder's action as
    the other's.  Every decision dict has exactly one ladder payload
    populated (or none, for an unclear timeout).
    """
    if decision.route not in VALID_ROUTES:
        raise FailureOrchestratorError(
            f"Invariant: route {decision.route!r} not in {sorted(VALID_ROUTES)}"
        )
    if decision.resolution not in {
        RESOLUTION_CONTENT, RESOLUTION_INFRA, RESOLUTION_UNCLEAR_TIMEOUT,
    }:
        raise FailureOrchestratorError(
            f"Invariant: resolution {decision.resolution!r} invalid"
        )

    if decision.route == ROUTE_CONTENT_LADDER:
        if decision.resolution != RESOLUTION_CONTENT:
            raise FailureOrchestratorError(
                f"Invariant: content route requires resolution=content, "
                f"got {decision.resolution!r}"
            )
        if decision.creative_action is None:
            raise FailureOrchestratorError(
                "Invariant: content route must carry a creative_action"
            )
        if decision.infra_action is not None:
            raise FailureOrchestratorError(
                "Invariant: content route must NOT carry an infra_action "
                "(silent downgrade detected)"
            )
        action_name = decision.creative_action.get("action")
        if action_name not in ACTION_NAMES:
            raise FailureOrchestratorError(
                f"Invariant: content route produced non-creative action "
                f"{action_name!r}; creative menu is {sorted(ACTION_NAMES)}"
            )

    elif decision.route == ROUTE_INFRA_LADDER:
        if decision.resolution != RESOLUTION_INFRA:
            raise FailureOrchestratorError(
                f"Invariant: infra route requires resolution=infra, "
                f"got {decision.resolution!r}"
            )
        if decision.creative_action is not None:
            raise FailureOrchestratorError(
                "Invariant: infra route must NOT carry a creative_action "
                "(silent downgrade detected)"
            )
        if decision.infra_action is None:
            raise FailureOrchestratorError(
                "Invariant: infra route must carry an infra_action"
            )

    else:  # ROUTE_HUMAN_ESCALATION
        if decision.escalation_id is None:
            raise FailureOrchestratorError(
                "Invariant: human-escalation route requires an escalation_id "
                "(L4 gate contract; never silently drop)"
            )
        if (
            decision.resolution == RESOLUTION_UNCLEAR_TIMEOUT
            and decision.creative_action is not None
        ):
            raise FailureOrchestratorError(
                "Invariant: unclear-timeout must not carry a creative_action"
            )
        if (
            decision.resolution == RESOLUTION_UNCLEAR_TIMEOUT
            and decision.infra_action is not None
        ):
            raise FailureOrchestratorError(
                "Invariant: unclear-timeout must not carry an infra_action"
            )


def _summarise_decision(decision: OrchestratorDecision) -> str:
    """Short one-liner suitable for a ``output_key`` blackboard slot."""
    if decision.route == ROUTE_CONTENT_LADDER and decision.creative_action:
        return (
            f"FAILURE_ROUTE: content -> "
            f"{decision.creative_action['action']}"
        )
    if decision.route == ROUTE_INFRA_LADDER and decision.infra_action:
        return (
            f"FAILURE_ROUTE: infra -> "
            f"{decision.infra_action['action_type']} "
            f"(L{decision.infra_terminal_level})"
        )
    if decision.route == ROUTE_HUMAN_ESCALATION:
        return (
            f"FAILURE_ROUTE: {decision.resolution} -> human_escalation "
            f"(id={decision.escalation_id})"
        )
    return f"FAILURE_ROUTE: {decision.route}"


# ---------------------------------------------------------------------------
# ADK surface — lazy so google-adk remains optional
# ---------------------------------------------------------------------------


_adk_build_lock = threading.Lock()
_adk_built = False
_failure_orchestrator_agent: Any = None  # BaseAgent at runtime


def _coerce_failure_event(raw: Any) -> FailureEvent:
    if isinstance(raw, FailureEvent):
        return raw
    if isinstance(raw, dict):
        return FailureEvent(**raw)
    raise FailureOrchestratorError(
        "state['failure_event'] must be FailureEvent or dict, got "
        f"{type(raw).__name__}"
    )


def _coerce_infra_telemetry(raw: Any) -> Optional[InfraTelemetry]:
    if raw is None:
        return None
    if isinstance(raw, InfraTelemetry):
        return raw
    if isinstance(raw, dict):
        return InfraTelemetry(**raw)
    raise FailureOrchestratorError(
        "state['infra_telemetry'] must be InfraTelemetry or dict, got "
        f"{type(raw).__name__}"
    )


def _build_adk_agent() -> Any:
    """Build the ADK ``BaseAgent`` wrapper for the failure orchestrator.

    The agent is a thin ADK adapter around :func:`route_failure` — it
    reads the failure event / telemetry from the session state, runs
    the orchestrator, writes the structured result + summary to the
    blackboard, and yields one ADK event per phase so the escalation
    shows up in the ADK trace view.

    An ``after_agent_callback`` enforces the invariants in
    :func:`_assert_no_silent_downgrade` at the stage boundary (Timeline
    Guardian pattern).
    """
    global _adk_built, _failure_orchestrator_agent
    with _adk_build_lock:
        if _adk_built:
            return _failure_orchestrator_agent
        _adk_built = True
        try:
            from google.adk.agents import BaseAgent
            from google.adk.agents.invocation_context import InvocationContext
            from google.adk.events.event import Event
            from google.genai import types as genai_types
        except Exception as exc:
            logger.warning(
                "FailureOrchestrator: google-adk not importable (%s); ADK "
                "surface disabled. route_failure() remains callable.",
                exc,
            )
            _failure_orchestrator_agent = None
            return None

        def _event_for(name: str, payload: dict[str, Any]) -> Event:
            return Event(
                author=name,
                content=genai_types.Content(
                    parts=[genai_types.Part(text=str(payload))],
                    role="model",
                ),
            )

        def _after_agent_callback(callback_context: Any) -> None:
            """Stage-boundary invariant guard (Timeline Guardian pattern).

            Raises ``FailureOrchestratorError`` on any invariant violation
            so the pipeline stops loud rather than continuing with a
            silently-downgraded decision.
            """
            state = getattr(callback_context, "state", None)
            if state is None:
                return
            raw = state.get(BLACKBOARD_RESULT_KEY) if hasattr(state, "get") else None
            if raw is None:
                # The agent didn't finalise — either it raised or it was
                # cancelled.  Either way we leave the caller to deal with
                # the absence; we don't invent a result.
                return
            if not isinstance(raw, dict):
                raise FailureOrchestratorError(
                    f"Invariant: {BLACKBOARD_RESULT_KEY} must be dict, "
                    f"got {type(raw).__name__}"
                )
            try:
                decision = OrchestratorDecision(
                    resolution=str(raw.get("resolution", "")),
                    route=str(raw.get("route", "")),
                    classification=dict(raw.get("classification") or {}),
                    creative_action=(
                        dict(raw["creative_action"])
                        if raw.get("creative_action") is not None
                        else None
                    ),
                    infra_action=(
                        dict(raw["infra_action"])
                        if raw.get("infra_action") is not None
                        else None
                    ),
                    infra_terminal_level=raw.get("infra_terminal_level"),
                    escalation_id=raw.get("escalation_id"),
                    diagnostic_trail=list(raw.get("diagnostic_trail") or []),
                )
            except (TypeError, ValueError) as exc:
                raise FailureOrchestratorError(
                    f"Invariant: orchestrator result failed to rehydrate: {exc}"
                ) from exc
            _assert_no_silent_downgrade(decision)

        class FailureOrchestratorAgent(BaseAgent):  # type: ignore[misc,valid-type]
            """ADK wrapper around :func:`route_failure`.

            Reads ``state['failure_event']`` (required) and
            ``state['infra_telemetry']`` (optional) from the blackboard
            and writes:

                state['failure_orchestrator_result']    # dict
                state['failure_orchestrator_summary']   # str
                state['failure_orchestrator_diagnostic_trail']  # list[dict]

            The write to ``BLACKBOARD_SUMMARY_KEY`` is what the ADK
            ``output_key`` mechanism would produce for a plain LLM
            Agent; we mimic that shape so downstream SequentialAgent
            steps can read the summary uniformly.
            """

            model_config = {"arbitrary_types_allowed": True}

            async def _run_async_impl(self, ctx: "InvocationContext"):
                session_state = ctx.session.state if ctx.session else {}

                raw_event = session_state.get(BLACKBOARD_FAILURE_EVENT_KEY)
                if raw_event is None:
                    raise FailureOrchestratorError(
                        "FailureOrchestrator ADK agent: state["
                        f"{BLACKBOARD_FAILURE_EVENT_KEY!r}] is unset. The "
                        "caller must populate the failure event before the "
                        "agent runs — the orchestrator refuses to silently no-op."
                    )
                event = _coerce_failure_event(raw_event)
                telemetry = _coerce_infra_telemetry(
                    session_state.get(BLACKBOARD_INFRA_TELEMETRY_KEY)
                )

                yield _event_for(self.name, {
                    "phase": "orchestrator_started",
                    "operation": event.operation_name,
                    "has_telemetry": telemetry is not None,
                })

                decision = route_failure(
                    event,
                    telemetry,
                    state=session_state,
                )

                yield _event_for(self.name, {
                    "phase": "orchestrator_finished",
                    "resolution": decision.resolution,
                    "route": decision.route,
                    "escalation_id": decision.escalation_id,
                })

        agent = FailureOrchestratorAgent(
            name="failure_orchestrator",
            after_agent_callback=_after_agent_callback,
        )
        _failure_orchestrator_agent = agent
        return agent


def get_failure_orchestrator_agent() -> Any:
    """Return the module-level ADK agent, building it lazily.

    Returns ``None`` if ``google-adk`` isn't importable in the current
    environment — in that case :func:`route_failure` remains the
    canonical entry point.
    """
    return _build_adk_agent()


# Test-only hook: force a rebuild of the ADK agent on next access.
def _reset_adk_agent_for_tests() -> None:
    global _adk_built, _failure_orchestrator_agent
    with _adk_build_lock:
        _adk_built = False
        _failure_orchestrator_agent = None


__all__ = [
    # Types
    "FailureEvent",
    "InfraTelemetry",
    "OrchestratorDecision",
    "FailureOrchestratorDeps",
    "FailureOrchestratorError",
    # Routes / resolutions
    "ROUTE_CONTENT_LADDER",
    "ROUTE_INFRA_LADDER",
    "ROUTE_HUMAN_ESCALATION",
    "VALID_ROUTES",
    "RESOLUTION_CONTENT",
    "RESOLUTION_INFRA",
    "RESOLUTION_UNCLEAR_TIMEOUT",
    "MAX_DIAGNOSTIC_ROUNDS",
    # Blackboard keys
    "BLACKBOARD_FAILURE_EVENT_KEY",
    "BLACKBOARD_INFRA_TELEMETRY_KEY",
    "BLACKBOARD_RESULT_KEY",
    "BLACKBOARD_SUMMARY_KEY",
    "BLACKBOARD_DIAGNOSTIC_TRAIL_KEY",
    # Entry points
    "route_failure",
    "build_default_deps",
    "get_failure_orchestrator_agent",
]
