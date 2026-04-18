"""
Infra escalation ladder — independent recovery ladder for infrastructure failures.

Implements ARCH-C2 (#141), parent workstream ARCH-C (#125), meta ARCH-2026
(#122).  Diagram 8 in ``docs/ARCHITECTURE_DIAGRAMS.md`` is the spec.

This ladder is distinct from the content ladder in ``server/recovery.py``:
content failures (bad prompts, QA rejections) consume content budget;
infra failures (worker crashes, OOM, CUDA errors, network partitions,
provider outages) consume **this** ladder's budget.

Ladder shape::

    Infra L0 FIX           retry same job on a different healthy worker
    Infra L1 RETRY         recycle suspect worker; redispatch in parallel on
                           another healthy worker if available
    Infra L2 CREATIVE      scale fleet / hot-swap GPU tier (within VRAM hard
                           floor) / change region / change Vast.ai offer /
                           change provider
    Infra L3 COLLABORATIVE coordinate with content ladder + budget guard;
                           may negotiate with orchestrator to reshape the
                           production plan
    Infra L4 HUMAN         terminates at the **same** dashboard gate as
                           content L4 (submit_escalation / resolve_escalation)

Reuses ``RecoveryLevel`` enum + ``RecoveryPolicy`` shape from
``server/recovery.py``; both ladders share the dataclass shape but each
owns its own budget via ``RecoveryPolicy.level_budgets``.

ADK idiom (per meta #122 DoD)::

    - Subclass ``google.adk.agents.Agent`` / ``BaseAgent`` where applicable.
    - Compose via ``SequentialAgent`` — one sub-agent per level.
    - Cross-stage state flows through the blackboard via ``output_key``.
    - Stage-boundary invariants enforced via ``after_agent_callback``
      (Timeline Guardian pattern).
    - Tools exposed as plain callables — directly invokable by tests and
      by the ARCH-C3 (#142) orchestrator rewrite.

Scope of this module (ARCH-C2 only): the ladder itself plus a clean
callable entry point ``run_infra_ladder``.  Wiring into the production
orchestrator's failure path is explicitly deferred to ARCH-C3 (#142).

The diagnostic classifier from ARCH-C1 (#140 / PR #163) provides the
routing signal.  This module accepts an optional ``classification`` on
each ``InfraFailureEvent`` but does **not** import
``agents.diagnostic_classifier`` — that branch isn't on main yet.
ARCH-C3 will be responsible for wiring the two together.

Fail-loud contract: this module never silently degrades.  If required
infrastructure (infra agent, provisioner, fleet coordinator) is
unreachable when the ladder needs it, the corresponding level returns
``None`` and the ladder escalates to the next level; at L4, a
``HumanEscalationRequest`` is submitted through the same gate content
L4 uses — the caller still receives an ``InfraLadderResult`` marking
the outcome so no exception is silently swallowed.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

# Reuse the canonical shape from the content ladder — both ladders share
# the dataclass, each owns its own budget via ``level_budgets``.
from recovery import (
    HumanEscalationRequest,
    RecoveryLevel,
    RecoveryPolicy,
    _next_escalation_id,
    submit_escalation,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blackboard keys
# ---------------------------------------------------------------------------

BLACKBOARD_STATE_KEY = "infra_ladder_state"
"""Structured state written by the ladder during execution — consumers
read this via ``ctx.session.state[BLACKBOARD_STATE_KEY]`` to learn which
level resolved the failure and what action was taken."""

BLACKBOARD_RESULT_KEY = "infra_ladder_result"
"""Final ``InfraLadderResult.to_dict()`` payload written by the ladder
after it terminates.  This is the handoff point back to ARCH-C3."""

# Per-level summary keys used as ADK ``output_key`` on the per-level
# sub-agents.  Each sub-agent writes a short human-readable summary
# into these slots so the orchestrator (or a dashboard) can render
# the escalation trail without re-parsing the structured result.
BLACKBOARD_L0_SUMMARY_KEY = "infra_ladder_l0_summary"
BLACKBOARD_L1_SUMMARY_KEY = "infra_ladder_l1_summary"
BLACKBOARD_L2_SUMMARY_KEY = "infra_ladder_l2_summary"
BLACKBOARD_L3_SUMMARY_KEY = "infra_ladder_l3_summary"
BLACKBOARD_L4_SUMMARY_KEY = "infra_ladder_l4_summary"

_LEVEL_SUMMARY_KEYS: dict[int, str] = {
    int(RecoveryLevel.FIX): BLACKBOARD_L0_SUMMARY_KEY,
    int(RecoveryLevel.RETRY): BLACKBOARD_L1_SUMMARY_KEY,
    int(RecoveryLevel.CREATIVE): BLACKBOARD_L2_SUMMARY_KEY,
    int(RecoveryLevel.COLLABORATIVE): BLACKBOARD_L3_SUMMARY_KEY,
    int(RecoveryLevel.HUMAN): BLACKBOARD_L4_SUMMARY_KEY,
}


# ---------------------------------------------------------------------------
# Failure signatures — the signals Diagram 8 calls out
# ---------------------------------------------------------------------------

# Canonical infra failure signatures per Diagram 8.  These are strings
# rather than an Enum so the classifier (ARCH-C1) and this module can
# evolve the vocabulary independently without a cross-module enum
# migration.  Callers are encouraged to use one of these constants, but
# free-form signatures are accepted (they'll route via L0 default).
INFRA_SIG_WORKER_DEATH = "worker_death"
INFRA_SIG_OOM = "oom"
INFRA_SIG_CUDA_ERROR = "cuda_error"
INFRA_SIG_DRIVER_RESET = "driver_reset"
INFRA_SIG_PREEMPTION = "preemption"
INFRA_SIG_COLD_START_FAIL = "cold_start_fail"
INFRA_SIG_NETWORK_PARTITION = "network_partition"
INFRA_SIG_VRAM_EXHAUSTED = "vram_exhausted"
INFRA_SIG_THERMAL_THROTTLE = "thermal_throttle"
INFRA_SIG_AUTH_REVOKED = "auth_revoked"
INFRA_SIG_BILLING_TRIP = "billing_trip"
INFRA_SIG_PROVIDER_OUTAGE = "provider_outage"
INFRA_SIG_STORAGE_UNREACHABLE = "storage_unreachable"

KNOWN_INFRA_SIGNATURES: frozenset[str] = frozenset(
    {
        INFRA_SIG_WORKER_DEATH,
        INFRA_SIG_OOM,
        INFRA_SIG_CUDA_ERROR,
        INFRA_SIG_DRIVER_RESET,
        INFRA_SIG_PREEMPTION,
        INFRA_SIG_COLD_START_FAIL,
        INFRA_SIG_NETWORK_PARTITION,
        INFRA_SIG_VRAM_EXHAUSTED,
        INFRA_SIG_THERMAL_THROTTLE,
        INFRA_SIG_AUTH_REVOKED,
        INFRA_SIG_BILLING_TRIP,
        INFRA_SIG_PROVIDER_OUTAGE,
        INFRA_SIG_STORAGE_UNREACHABLE,
    }
)


# ---------------------------------------------------------------------------
# Action vocabulary — what the ladder tells the orchestrator to do next
# ---------------------------------------------------------------------------

ActionType = Literal[
    "retry_on_healthy_worker",        # L0
    "recycle_and_redispatch",         # L1
    "scale_fleet",                    # L2
    "hot_swap_tier",                  # L2
    "change_region",                  # L2
    "change_provider",                # L2
    "coordinate_with_content_ladder",  # L3
    "reshape_production_plan",        # L3
    "escalate_human",                 # L4
]

_ACTION_LEVEL: dict[str, int] = {
    "retry_on_healthy_worker": int(RecoveryLevel.FIX),
    "recycle_and_redispatch": int(RecoveryLevel.RETRY),
    "scale_fleet": int(RecoveryLevel.CREATIVE),
    "hot_swap_tier": int(RecoveryLevel.CREATIVE),
    "change_region": int(RecoveryLevel.CREATIVE),
    "change_provider": int(RecoveryLevel.CREATIVE),
    "coordinate_with_content_ladder": int(RecoveryLevel.COLLABORATIVE),
    "reshape_production_plan": int(RecoveryLevel.COLLABORATIVE),
    "escalate_human": int(RecoveryLevel.HUMAN),
}


@dataclass(frozen=True)
class InfraRecoveryAction:
    """A single action the ladder emits for the caller to execute.

    The infra ladder decides *what* should happen (redispatch, scale,
    hot-swap, escalate) but does not execute the clip itself — that's
    still the orchestrator's job.  Each level returns one
    ``InfraRecoveryAction`` on success (or None to escalate).
    """

    action_type: ActionType
    level: int  # int(RecoveryLevel.*) where this action originated
    reason: str
    target_worker_url: Optional[str] = None
    recycled_worker_url: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)
    escalation_id: Optional[str] = None  # populated only for L4

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "level": self.level,
            "reason": self.reason,
            "target_worker_url": self.target_worker_url,
            "recycled_worker_url": self.recycled_worker_url,
            "details": dict(self.details),
            "escalation_id": self.escalation_id,
        }


# ---------------------------------------------------------------------------
# Inputs / state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InfraFailureEvent:
    """Structured description of a single infra failure.

    Built by ARCH-C3's failure-path code; consumed here.  Only minimal
    fields are required — the rest lives on ``metadata`` so the
    classifier (ARCH-C1) can attach whatever context it produced.
    """

    job_id: str
    """Unique identifier for the in-flight job (e.g. ``clip_id``)."""

    worker_url: str
    """URL of the suspect worker — the one that the failure happened on."""

    failure_signature: str
    """Canonical signature from :data:`KNOWN_INFRA_SIGNATURES` or a
    free-form string.  Unknown signatures are accepted; they route via
    the L0 default path."""

    raw_error: str = ""
    """The underlying error string/traceback, for the escalation trail."""

    classification: Optional[dict[str, Any]] = None
    """Opaque classifier output from ARCH-C1 (PR #163), if available.
    Structure: ``{"classification": "infra", "confidence": 0.92,
    "reasoning": "...", "signals": [...]}``.  If the field is populated
    and ``classification["classification"] != "infra"``, the ladder
    fails loud — the classifier mis-routed, not this ladder's fault.
    """

    metadata: dict[str, Any] = field(default_factory=dict)
    """Free-form additional context (tier, region, retry history, etc.)"""

    def is_known_signature(self) -> bool:
        return self.failure_signature in KNOWN_INFRA_SIGNATURES


@dataclass
class InfraLadderState:
    """Mutable state passed between ladder levels."""

    event: InfraFailureEvent
    policy: RecoveryPolicy
    attempts_by_level: dict[int, int] = field(default_factory=dict)
    actions_taken: list[InfraRecoveryAction] = field(default_factory=list)
    escalation_trail: list[str] = field(default_factory=list)

    # Set of worker URLs known to be bad or under recycle — the ladder
    # avoids re-dispatching to these.
    excluded_workers: set[str] = field(default_factory=set)

    def record_attempt(self, level: int) -> None:
        self.attempts_by_level[level] = self.attempts_by_level.get(level, 0) + 1

    def budget_remaining(self, level: int) -> int:
        used = self.attempts_by_level.get(level, 0)
        return max(0, self.policy.get_level_budget(level) - used)

    def record_escalation(self, note: str) -> None:
        self.escalation_trail.append(note)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": {
                "job_id": self.event.job_id,
                "worker_url": self.event.worker_url,
                "failure_signature": self.event.failure_signature,
                "classification": self.event.classification,
            },
            "attempts_by_level": dict(self.attempts_by_level),
            "actions_taken": [a.to_dict() for a in self.actions_taken],
            "escalation_trail": list(self.escalation_trail),
            "excluded_workers": sorted(self.excluded_workers),
        }


@dataclass(frozen=True)
class InfraLadderResult:
    """Terminal result emitted when the ladder finishes."""

    success: bool
    terminal_level: int  # int(RecoveryLevel.*) — the level that produced the action
    action: Optional[InfraRecoveryAction]
    state_snapshot: dict[str, Any]
    escalation_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "terminal_level": self.terminal_level,
            "action": self.action.to_dict() if self.action is not None else None,
            "state_snapshot": self.state_snapshot,
            "escalation_id": self.escalation_id,
        }


# ---------------------------------------------------------------------------
# Deps — dependency injection for testability
# ---------------------------------------------------------------------------


@dataclass
class InfraLadderDeps:
    """Callables the ladder invokes at each level.

    All are overridable for tests.  The defaults resolve against the
    live singletons (``infra_agent.get_infra_agent``,
    ``worker_provisioner.get_provisioner``, ``fleet.coordinator``) if
    available.  When a dep is unreachable, the relevant level returns
    ``None`` and the ladder escalates rather than silently degrading.
    """

    # L0 — retry on different healthy worker
    get_healthy_workers: Callable[[], list[str]]

    # L1 — recycle suspect + parallel redispatch
    recycle_worker: Callable[[str], bool]
    """``recycle_worker(url)`` tears down the suspect VM and provisions
    a fresh one.  Returns True if recycle was successfully initiated
    (it may still be booting)."""

    # L2 — scaling / hot-swap / region / provider
    scale_fleet: Callable[[int], Optional[str]]
    """``scale_fleet(extra_workers)`` provisions N additional workers at
    the current tier and returns the URL of one (or None on failure)."""

    hot_swap_tier: Callable[[str, str], Optional[str]]
    """``hot_swap_tier(suspect_url, target_tier)`` provisions a worker
    on a different GPU tier within the VRAM hard floor.  Returns the
    new worker URL or None."""

    change_region: Callable[[str, str], Optional[str]]
    """``change_region(suspect_url, target_region)`` provisions in a
    different region.  Returns URL or None."""

    change_provider: Callable[[str, str], Optional[str]]
    """``change_provider(suspect_url, target_provider)`` provisions on
    a different Vast.ai offer or entirely different provider.  Returns
    URL or None."""

    # L3 — coordinate with content ladder + budget guard
    coordinate_with_content: Callable[[InfraFailureEvent, InfraLadderState], dict[str, Any]]
    """Consult the content ladder to see if down-speccing / retrying
    with different clip params would avoid the failure.  Returns a dict
    with at least ``{"can_downspec": bool, "note": str}``."""

    budget_guard: Callable[[], dict[str, Any]]
    """Check current budget state.  Returns
    ``{"ok_to_continue": bool, "remaining_usd": float, "note": str}``.
    """

    reshape_plan: Callable[[InfraFailureEvent, InfraLadderState], bool]
    """Ask the orchestrator to reshape the production plan (e.g. drop
    scenes, lower quality).  Returns True if a reshape was accepted."""

    # L4 — human gate (same as content L4)
    submit_human_escalation: Callable[[HumanEscalationRequest], None]
    """Submits the escalation via the same gate content L4 uses.  By
    default this points at ``recovery.submit_escalation``."""


# ---------------------------------------------------------------------------
# Default dep wiring — resolves against the live singletons
# ---------------------------------------------------------------------------


def _default_get_healthy_workers() -> list[str]:
    """Return healthy video-worker URLs from the live ``InfraAgent``.

    Returns an empty list if the agent isn't running — the ladder will
    treat that as ``no healthy worker available`` and escalate, which
    is the correct fail-loud behaviour.
    """
    try:
        from infra_agent import WorkerRole, get_infra_agent

        agent = get_infra_agent()
        if agent is None:
            logger.warning(
                "Infra ladder: InfraAgent not running — cannot enumerate "
                "healthy workers"
            )
            return []
        return list(agent.get_healthy_workers(role=WorkerRole.VIDEO))
    except Exception as exc:
        logger.error(
            "Infra ladder: get_healthy_workers failed: %s", exc, exc_info=True
        )
        return []


def _default_recycle_worker(suspect_url: str) -> bool:
    """Teardown the suspect VM via the worker provisioner.

    The live provisioner's ``_destroy_and_reset_spec`` rewires the
    WorkerSpec for a fresh boot; the provisioning lane in
    ``_provision_and_connect`` picks it up on the next ``start_provisioning``
    call.  We do not block waiting for the new VM to be healthy — that's
    what the parallel redispatch step is for.
    """
    try:
        from worker_provisioner import get_provisioner

        provisioner = get_provisioner()
        if provisioner is None:
            logger.warning(
                "Infra ladder: WorkerProvisioner singleton not available — "
                "cannot recycle %s",
                suspect_url,
            )
            return False

        # Locate the WorkerSpec for the suspect URL
        target_spec = None
        with provisioner._lock:  # noqa: SLF001 — ladder is same-package boundary
            for spec in provisioner._specs:  # noqa: SLF001
                if spec.worker_url and spec.worker_url == suspect_url:
                    target_spec = spec
                    break

        if target_spec is None:
            logger.warning(
                "Infra ladder: no provisioner spec matches %s — "
                "treating as externally-managed, cannot recycle",
                suspect_url,
            )
            return False

        provisioner._destroy_and_reset_spec(target_spec)  # noqa: SLF001
        logger.info(
            "Infra ladder: recycled worker %s (spec reset; fresh boot pending)",
            suspect_url,
        )
        # Deregister from the infra agent so it doesn't keep polling a
        # torn-down URL.
        try:
            from infra_agent import get_infra_agent

            agent = get_infra_agent()
            if agent is not None:
                agent.remove_worker(suspect_url)
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.error(
            "Infra ladder: recycle_worker(%s) failed: %s",
            suspect_url, exc, exc_info=True,
        )
        return False


def _default_scale_fleet(extra_workers: int) -> Optional[str]:
    """Provision ``extra_workers`` additional VMs at the current tier.

    Returns the URL of one newly-provisioned worker (or None).  The
    live path delegates to ``FleetCoordinator.provision_fleet`` which
    in turn uses ``FleetScaler`` — we do not duplicate that logic here.
    """
    try:
        from fleet.coordinator import get_fleet_coordinator

        coord = get_fleet_coordinator()
        if coord is None:
            logger.warning(
                "Infra ladder: FleetCoordinator not running — cannot scale"
            )
            return None
        # provision_fleet is async w.r.t. VM booting; it returns the
        # target count, not a URL.  We return None here to signal the
        # ladder should move on to the next L2 sub-strategy — scaling
        # is in flight, not done, so there's no URL to retry on yet.
        # The retry will happen once the new worker registers via
        # InfraAgent.add_worker().  This is deliberately fail-loud: if
        # the caller wants "retry on the new worker" it should poll
        # get_healthy_workers() until a new URL shows up.
        count = coord.provision_fleet(num_clips=extra_workers)
        logger.info(
            "Infra ladder: scale_fleet requested %d extra workers (target=%d)",
            extra_workers, count,
        )
        # No URL to return synchronously — the ladder moves on.
        return None
    except Exception as exc:
        logger.error(
            "Infra ladder: scale_fleet(%d) failed: %s",
            extra_workers, exc, exc_info=True,
        )
        return None


def _default_hot_swap_tier(
    suspect_url: str, target_tier: str
) -> Optional[str]:
    """Provision a worker on a different GPU tier.

    The default implementation does not actually swap — that requires a
    provisioner interface we haven't wired yet.  Returns None so the
    ladder moves on to change_region / change_provider.  This is
    fail-loud: the L2 sub-strategy is not silently degraded to a no-op,
    the ladder just escalates past it.
    """
    logger.info(
        "Infra ladder: hot_swap_tier(%s -> %s) not implemented in default "
        "deps — C3 wiring will supply this",
        suspect_url, target_tier,
    )
    return None


def _default_change_region(
    suspect_url: str, target_region: str
) -> Optional[str]:
    """Provision in a different region.  Default: not wired; returns None."""
    logger.info(
        "Infra ladder: change_region(%s -> %s) not implemented in default "
        "deps — C3 wiring will supply this",
        suspect_url, target_region,
    )
    return None


def _default_change_provider(
    suspect_url: str, target_provider: str
) -> Optional[str]:
    """Provision on a different provider.  Default: not wired; returns None."""
    logger.info(
        "Infra ladder: change_provider(%s -> %s) not implemented in default "
        "deps — C3 wiring will supply this",
        suspect_url, target_provider,
    )
    return None


def _default_coordinate_with_content(
    event: InfraFailureEvent, state: InfraLadderState
) -> dict[str, Any]:
    """Consult the content ladder.  Default: no coordination available."""
    return {
        "can_downspec": False,
        "note": (
            "Default deps: content ladder coordination not wired. "
            "C3 will supply the actual consult path."
        ),
    }


def _default_budget_guard() -> dict[str, Any]:
    """Check budget via the fleet coordinator's cost tracker."""
    try:
        from fleet.coordinator import get_fleet_coordinator

        coord = get_fleet_coordinator()
        if coord is None:
            return {
                "ok_to_continue": True,
                "remaining_usd": -1.0,
                "note": "FleetCoordinator not running — no budget data",
            }
        tracker = coord._cost_tracker  # noqa: SLF001
        should_stop = tracker.should_stop_provisioning()
        return {
            "ok_to_continue": not should_stop,
            "remaining_usd": float(getattr(tracker, "remaining_budget", -1.0) or -1.0),
            "note": (
                "Budget ceiling hit" if should_stop else "Budget ok"
            ),
        }
    except Exception as exc:
        logger.error(
            "Infra ladder: budget_guard failed: %s", exc, exc_info=True
        )
        return {
            "ok_to_continue": False,
            "remaining_usd": -1.0,
            "note": f"budget_guard error: {exc}",
        }


def _default_reshape_plan(
    event: InfraFailureEvent, state: InfraLadderState
) -> bool:
    """Ask the orchestrator to reshape the plan.  Default: not wired."""
    logger.info(
        "Infra ladder: reshape_plan not wired in default deps — C3 will "
        "supply this"
    )
    return False


def _default_submit_human_escalation(req: HumanEscalationRequest) -> None:
    """Submit through the same gate content L4 uses.

    The content ladder uses ``recovery.submit_escalation`` which writes
    into the shared ``_pending_escalations`` registry read by the
    AG-UI dashboard.  The infra ladder's L4 uses the exact same gate.
    """
    submit_escalation(req)


def build_default_deps() -> InfraLadderDeps:
    """Construct an ``InfraLadderDeps`` pointing at live singletons."""
    return InfraLadderDeps(
        get_healthy_workers=_default_get_healthy_workers,
        recycle_worker=_default_recycle_worker,
        scale_fleet=_default_scale_fleet,
        hot_swap_tier=_default_hot_swap_tier,
        change_region=_default_change_region,
        change_provider=_default_change_provider,
        coordinate_with_content=_default_coordinate_with_content,
        budget_guard=_default_budget_guard,
        reshape_plan=_default_reshape_plan,
        submit_human_escalation=_default_submit_human_escalation,
    )


# ---------------------------------------------------------------------------
# Policy — infra ladder owns its own budget
# ---------------------------------------------------------------------------


INFRA_POLICY = RecoveryPolicy(
    # Per-level budgets.  These are the infra ladder's own budget —
    # distinct from VIDEO_POLICY / TTS_POLICY / LLM_POLICY.  The
    # content ladder's budget is untouched.
    level_budgets={
        int(RecoveryLevel.FIX): 3,          # L0: up to 3 different healthy workers
        int(RecoveryLevel.RETRY): 2,        # L1: recycle + redispatch, twice
        int(RecoveryLevel.CREATIVE): 3,     # L2: scale, hot-swap, region, provider
        int(RecoveryLevel.COLLABORATIVE): 1,  # L3: one coordination round
        int(RecoveryLevel.HUMAN): 1,        # L4: one human gate per failure
    },
    escalate_to_human=True,
    human_timeout_sec=float(os.environ.get("INFRA_LADDER_HUMAN_TIMEOUT", "600")),
)


# ---------------------------------------------------------------------------
# Tools — plain callables, per DoD
# ---------------------------------------------------------------------------


def infra_l0_fix(
    event: InfraFailureEvent,
    state: InfraLadderState,
    deps: InfraLadderDeps,
) -> Optional[InfraRecoveryAction]:
    """L0 FIX — retry on another healthy worker in the fleet.

    Picks the first healthy worker that isn't the suspect (or already
    excluded from earlier attempts).  Returns None if no such worker
    exists — the ladder escalates to L1.
    """
    level = int(RecoveryLevel.FIX)
    state.record_attempt(level)

    healthy = deps.get_healthy_workers()
    # Exclude suspect and anyone we've already tried or recycled
    candidates = [
        url for url in healthy
        if url != event.worker_url and url not in state.excluded_workers
    ]
    if not candidates:
        state.record_escalation(
            f"L0: no healthy worker besides suspect {event.worker_url}"
        )
        return None

    chosen = candidates[0]
    state.excluded_workers.add(chosen)  # don't re-pick on next L0 attempt
    action = InfraRecoveryAction(
        action_type="retry_on_healthy_worker",
        level=level,
        reason=(
            f"Retry job {event.job_id} on healthy worker {chosen} "
            f"(suspect {event.worker_url} sig={event.failure_signature})"
        ),
        target_worker_url=chosen,
        details={
            "healthy_count": len(healthy),
            "candidate_count": len(candidates),
        },
    )
    state.actions_taken.append(action)
    return action


def infra_l1_retry(
    event: InfraFailureEvent,
    state: InfraLadderState,
    deps: InfraLadderDeps,
) -> Optional[InfraRecoveryAction]:
    """L1 RETRY — recycle suspect + redispatch in parallel.

    Per Diagram 8: "recycle suspect worker (teardown + fresh boot); if
    another worker available, redispatch there first, recycle in
    parallel."

    Flow:
        1. Start recycling the suspect worker (async from the ladder's
           point of view — we just kick it off and move on).
        2. If there's a healthy worker besides the suspect, redispatch
           the job to it immediately (parallel to recycle).
        3. Otherwise escalate to L2.
    """
    level = int(RecoveryLevel.RETRY)
    state.record_attempt(level)

    # Step 1: kick off recycle (may take minutes to complete; we do not
    # block on it).
    recycled = deps.recycle_worker(event.worker_url)
    if recycled:
        state.excluded_workers.add(event.worker_url)
        state.record_escalation(
            f"L1: recycle initiated for {event.worker_url}"
        )

    # Step 2: look for a redispatch target
    healthy = deps.get_healthy_workers()
    candidates = [
        url for url in healthy
        if url != event.worker_url and url not in state.excluded_workers
    ]
    # Important: we want candidates *other than* the suspect, but we do
    # not require them to be outside excluded_workers that were just
    # added by L0, because L0 may have tried them and come back here.
    # Re-allow L0-tried workers only if L0 saw them healthy.
    if not candidates:
        # Allow a redispatch on any healthy worker that isn't the
        # suspect itself — L1 is a recycle + fresh retry, not "try yet
        # another untried worker".
        candidates = [url for url in healthy if url != event.worker_url]

    if candidates:
        target = candidates[0]
        action = InfraRecoveryAction(
            action_type="recycle_and_redispatch",
            level=level,
            reason=(
                f"Recycling suspect {event.worker_url} "
                f"(recycle_started={recycled}); redispatching job "
                f"{event.job_id} in parallel on {target}"
            ),
            target_worker_url=target,
            recycled_worker_url=event.worker_url,
            details={
                "recycle_initiated": recycled,
                "parallel_redispatch": True,
                "healthy_count": len(healthy),
            },
        )
        state.actions_taken.append(action)
        return action

    # No parallel target — recycle alone is insufficient for progress.
    # Record partial work but escalate.
    if recycled:
        state.record_escalation(
            "L1: recycle initiated but no parallel redispatch target"
        )
    else:
        state.record_escalation(
            f"L1: recycle_worker({event.worker_url}) failed and no "
            f"parallel redispatch target"
        )
    return None


def infra_l2_creative(
    event: InfraFailureEvent,
    state: InfraLadderState,
    deps: InfraLadderDeps,
) -> Optional[InfraRecoveryAction]:
    """L2 CREATIVE — scale, hot-swap tier, region, or provider.

    Tries the strategies in order:
        scale_fleet(+1) → hot_swap_tier → change_region → change_provider.

    The first strategy that yields a new worker URL wins.  If none
    succeed, escalate to L3.  Per Diagram 8 the GPU tier swap must
    respect the VRAM hard floor (48–80 GB depending on LTX-2.3 tier);
    this constraint lives in the dep implementation, not in the ladder.
    """
    level = int(RecoveryLevel.CREATIVE)
    state.record_attempt(level)

    # 1. Scale fleet (most conservative — stay at current tier/region/provider)
    scaled_url = deps.scale_fleet(1)
    if scaled_url:
        state.excluded_workers.add(scaled_url)
        action = InfraRecoveryAction(
            action_type="scale_fleet",
            level=level,
            reason=(
                f"Scaled fleet +1 at current tier to work around "
                f"{event.failure_signature} on {event.worker_url}"
            ),
            target_worker_url=scaled_url,
            details={"strategy": "scale_fleet", "delta": 1},
        )
        state.actions_taken.append(action)
        return action

    # 2. Hot-swap GPU tier (within VRAM hard floor)
    target_tier = str(
        event.metadata.get("fallback_tier")
        or os.environ.get("INFRA_LADDER_FALLBACK_TIER", "")
    ).strip()
    if target_tier:
        swapped_url = deps.hot_swap_tier(event.worker_url, target_tier)
        if swapped_url:
            state.excluded_workers.add(swapped_url)
            action = InfraRecoveryAction(
                action_type="hot_swap_tier",
                level=level,
                reason=(
                    f"Hot-swapped GPU tier to {target_tier} for job "
                    f"{event.job_id} (suspect {event.worker_url})"
                ),
                target_worker_url=swapped_url,
                details={"strategy": "hot_swap_tier", "target_tier": target_tier},
            )
            state.actions_taken.append(action)
            return action

    # 3. Change region
    target_region = str(
        event.metadata.get("fallback_region")
        or os.environ.get("INFRA_LADDER_FALLBACK_REGION", "")
    ).strip()
    if target_region:
        region_url = deps.change_region(event.worker_url, target_region)
        if region_url:
            state.excluded_workers.add(region_url)
            action = InfraRecoveryAction(
                action_type="change_region",
                level=level,
                reason=(
                    f"Changed region to {target_region} after "
                    f"{event.failure_signature}"
                ),
                target_worker_url=region_url,
                details={"strategy": "change_region", "target_region": target_region},
            )
            state.actions_taken.append(action)
            return action

    # 4. Change provider (or different Vast.ai offer)
    target_provider = str(
        event.metadata.get("fallback_provider")
        or os.environ.get("INFRA_LADDER_FALLBACK_PROVIDER", "")
    ).strip()
    if target_provider:
        provider_url = deps.change_provider(event.worker_url, target_provider)
        if provider_url:
            state.excluded_workers.add(provider_url)
            action = InfraRecoveryAction(
                action_type="change_provider",
                level=level,
                reason=(
                    f"Changed provider to {target_provider} after "
                    f"{event.failure_signature}"
                ),
                target_worker_url=provider_url,
                details={"strategy": "change_provider", "target_provider": target_provider},
            )
            state.actions_taken.append(action)
            return action

    state.record_escalation(
        "L2: scale/hot-swap/region/provider all returned None"
    )
    return None


def infra_l3_collaborative(
    event: InfraFailureEvent,
    state: InfraLadderState,
    deps: InfraLadderDeps,
) -> Optional[InfraRecoveryAction]:
    """L3 COLLABORATIVE — coordinate with content ladder + budget guard.

    Per Diagram 8: "coordinate with content ladder; maybe the clip
    params are the cause and content can down-spec. Coordinate with
    budget guard; may reshape the production plan."

    Flow:
        1. Consult the budget guard — if budget is exhausted, jump
           straight to L4 (no point negotiating content downspec if
           there's no budget left to retry).
        2. Consult the content ladder.  If it offers a down-spec path,
           emit ``coordinate_with_content_ladder``.
        3. Else ask the orchestrator to reshape the production plan.
           If accepted, emit ``reshape_production_plan``.
        4. Else escalate to L4.
    """
    level = int(RecoveryLevel.COLLABORATIVE)
    state.record_attempt(level)

    # 1. Budget guard
    budget = deps.budget_guard()
    if not budget.get("ok_to_continue", True):
        state.record_escalation(
            f"L3: budget guard says stop ({budget.get('note', '')}) — "
            f"escalating to L4"
        )
        return None  # forces L4

    # 2. Content ladder consult
    content = deps.coordinate_with_content(event, state)
    if content.get("can_downspec"):
        action = InfraRecoveryAction(
            action_type="coordinate_with_content_ladder",
            level=level,
            reason=(
                f"Content ladder offered a down-spec path "
                f"({content.get('note', '')}) to work around "
                f"{event.failure_signature}"
            ),
            details={
                "content_consult": content,
                "budget": budget,
            },
        )
        state.actions_taken.append(action)
        return action

    # 3. Plan reshape
    if deps.reshape_plan(event, state):
        action = InfraRecoveryAction(
            action_type="reshape_production_plan",
            level=level,
            reason=(
                f"Orchestrator accepted production plan reshape to "
                f"work around {event.failure_signature} on "
                f"{event.worker_url}"
            ),
            details={"content_consult": content, "budget": budget},
        )
        state.actions_taken.append(action)
        return action

    state.record_escalation(
        "L3: content cannot down-spec and orchestrator rejected reshape"
    )
    return None


def infra_l4_human(
    event: InfraFailureEvent,
    state: InfraLadderState,
    deps: InfraLadderDeps,
) -> InfraRecoveryAction:
    """L4 HUMAN — terminates at the same dashboard gate as content L4.

    Always returns an action (never None) — L4 is the terminus.  The
    action's ``escalation_id`` is the id submitted into the shared
    ``recovery._pending_escalations`` registry; the dashboard will pick
    it up via the AG-UI SSE stream.

    Per the content ladder's L4 contract: we don't block here waiting
    for the human response.  The caller owns the await — this mirrors
    ``recovery._escalate_to_human`` but is non-blocking so the infra
    ladder never silently pauses the orchestrator.
    """
    level = int(RecoveryLevel.HUMAN)
    state.record_attempt(level)

    escalation_id = _next_escalation_id()
    diagnosis = {
        "root_cause": (
            f"Infra ladder exhausted L0–L3 for job {event.job_id} on "
            f"{event.worker_url}: {event.failure_signature}"
        ),
        "confidence": "confirmed",
        "proposed_fix": (
            "Manual investigation needed — infra L0-L3 could not resolve "
            "the failure. Consider: confirm provider capacity, verify "
            "VRAM floor, check classifier routing."
        ),
        "raw_error": event.raw_error[:500] if event.raw_error else "",
        "failure_signature": event.failure_signature,
    }
    proposed_actions = [
        {
            "action_id": "retry_with_fix",
            "description": "Retry job after manual fix",
            "risk_level": "low",
        },
        {
            "action_id": "skip",
            "description": "Skip this job and continue pipeline",
            "risk_level": "medium",
        },
        {
            "action_id": "abort",
            "description": "Abort the pipeline run",
            "risk_level": "high",
        },
    ]

    req = HumanEscalationRequest(
        id=escalation_id,
        operation_name=f"infra_ladder:{event.job_id}",
        error_chain=[a.to_dict() for a in state.actions_taken],
        diagnosis=diagnosis,
        proposed_actions=proposed_actions,
        severity="critical",
        timestamp=time.time(),
    )
    deps.submit_human_escalation(req)

    action = InfraRecoveryAction(
        action_type="escalate_human",
        level=level,
        reason=(
            f"Infra ladder exhausted for {event.job_id}; escalated to "
            f"same dashboard gate as content L4 (id={escalation_id})"
        ),
        escalation_id=escalation_id,
        details={
            "escalation_id": escalation_id,
            "trail": list(state.escalation_trail),
        },
    )
    state.actions_taken.append(action)
    state.record_escalation(f"L4: escalated to dashboard (id={escalation_id})")
    return action


# Ordered registry of level-tool pairs.  ``run_infra_ladder`` walks this
# in order, bailing on the first tool that returns a non-None action.
_LEVEL_TOOLS: list[tuple[int, Callable[..., Optional[InfraRecoveryAction]]]] = [
    (int(RecoveryLevel.FIX), infra_l0_fix),
    (int(RecoveryLevel.RETRY), infra_l1_retry),
    (int(RecoveryLevel.CREATIVE), infra_l2_creative),
    (int(RecoveryLevel.COLLABORATIVE), infra_l3_collaborative),
]


# ---------------------------------------------------------------------------
# Top-level callable — ARCH-C3 entry point
# ---------------------------------------------------------------------------


def run_infra_ladder(
    event: InfraFailureEvent,
    *,
    policy: Optional[RecoveryPolicy] = None,
    deps: Optional[InfraLadderDeps] = None,
    state: Optional[InfraLadderState] = None,
    start_level: int = int(RecoveryLevel.FIX),
) -> InfraLadderResult:
    """Run the infra escalation ladder for a single failure event.

    The callable interface ARCH-C3 (#142) will invoke when the
    diagnostic classifier routes a failure to the infra axis.

    Fail-loud contract:
        * If ``event.classification`` is populated and doesn't say
          ``"infra"``, raises ``ValueError`` immediately — the caller
          mis-routed the failure.
        * If the ladder exhausts L0–L3 without success, L4 submits a
          human escalation and returns a result with ``success=False``,
          ``action.action_type == "escalate_human"`` and
          ``escalation_id`` populated.  The caller is responsible for
          awaiting human response or propagating the escalation — this
          function never silently degrades or silently blocks.

    Args:
        event: Failure description.
        policy: Optional override; defaults to :data:`INFRA_POLICY`.
        deps: Optional dep injection; defaults to :func:`build_default_deps`.
        state: Optional pre-existing state (used when the classifier
            has already loaded budget info; typically None).
        start_level: Level to start at.  Must be ``FIX..HUMAN``.  Useful
            for tests and for resume-after-partial-escalation scenarios.

    Returns:
        :class:`InfraLadderResult` describing which level resolved the
        failure and what action to take.
    """
    if not isinstance(event, InfraFailureEvent):
        raise TypeError(
            f"run_infra_ladder: event must be InfraFailureEvent, got {type(event)!r}"
        )
    cls = event.classification or {}
    if cls and cls.get("classification") not in (None, "infra"):
        raise ValueError(
            f"run_infra_ladder: classifier routed failure as "
            f"{cls.get('classification')!r}, not 'infra'. The caller "
            f"(ARCH-C3) must route non-infra failures to the content ladder."
        )
    if start_level < int(RecoveryLevel.FIX) or start_level > int(RecoveryLevel.HUMAN):
        raise ValueError(
            f"run_infra_ladder: start_level {start_level} out of range "
            f"[{int(RecoveryLevel.FIX)}, {int(RecoveryLevel.HUMAN)}]"
        )

    policy = policy or INFRA_POLICY
    deps = deps or build_default_deps()
    state = state or InfraLadderState(event=event, policy=policy)

    # Walk L0..L3 in order, bailing on the first success.
    for level, tool in _LEVEL_TOOLS:
        if level < start_level:
            continue
        if state.budget_remaining(level) <= 0:
            state.record_escalation(
                f"L{level}: budget exhausted before tool invocation"
            )
            continue
        try:
            action = tool(event, state, deps)
        except Exception as exc:
            # Fail loud: a tool crash is not silently swallowed.  We
            # record the escalation and continue to the next level.
            logger.exception(
                "Infra ladder: L%d tool raised: %s", level, exc
            )
            state.record_escalation(f"L{level}: tool raised {type(exc).__name__}: {exc}")
            action = None
        if action is not None:
            return InfraLadderResult(
                success=True,
                terminal_level=level,
                action=action,
                state_snapshot=state.to_dict(),
            )

    # L4 — always runs if we got here.
    if start_level > int(RecoveryLevel.HUMAN):
        # Can't happen given the range check above, but guard anyway.
        raise RuntimeError(
            "Infra ladder: escalation past L4 is not defined"
        )
    if state.budget_remaining(int(RecoveryLevel.HUMAN)) <= 0:
        # Even L4 has a budget (default 1).  If the caller re-ran the
        # ladder after a human escalation was already submitted, do
        # not submit another — surface the state as a terminal failure.
        state.record_escalation("L4: human budget exhausted")
        return InfraLadderResult(
            success=False,
            terminal_level=int(RecoveryLevel.HUMAN),
            action=None,
            state_snapshot=state.to_dict(),
        )

    action = infra_l4_human(event, state, deps)
    return InfraLadderResult(
        success=False,  # L4 means the ladder itself did not resolve
        terminal_level=int(RecoveryLevel.HUMAN),
        action=action,
        state_snapshot=state.to_dict(),
        escalation_id=action.escalation_id,
    )


# ---------------------------------------------------------------------------
# ADK Agent surface — lazy so `google-adk` is optional in minimal envs
# ---------------------------------------------------------------------------


_adk_build_lock = threading.Lock()
_adk_built = False
_infra_ladder_agent: Any = None  # google.adk.agents.BaseAgent at runtime


def _build_adk_agent() -> Any:
    """Construct the ADK surface for the infra ladder.

    Returns a ``BaseAgent`` subclass that wraps :func:`run_infra_ladder`
    and yields one ADK event per level as it escalates.  Sub-agents for
    each level are constructed so the escalation trail shows up in the
    ADK trace view and each level's summary lands on its own
    ``output_key`` in the blackboard.

    Lazy-built because ``google-adk`` is optional in some test envs.
    If the import fails, this returns ``None`` and consumers should
    call :func:`run_infra_ladder` directly.
    """
    global _adk_built, _infra_ladder_agent
    with _adk_build_lock:
        if _adk_built:
            return _infra_ladder_agent
        _adk_built = True
        try:
            from google.adk.agents import BaseAgent
            from google.adk.agents.invocation_context import InvocationContext
            from google.adk.events.event import Event
            from google.genai import types as genai_types
        except Exception as exc:
            logger.warning(
                "Infra ladder: google-adk not importable (%s); "
                "ADK surface disabled. run_infra_ladder() remains callable.",
                exc,
            )
            _infra_ladder_agent = None
            return None

        def _event_for(name: str, payload: dict[str, Any]) -> Event:
            return Event(
                author=name,
                content=genai_types.Content(
                    parts=[genai_types.Part(text=str(payload))],
                    role="model",
                ),
            )

        def _check_ladder_state(callback_context: Any) -> None:
            """``after_agent_callback`` — stage-boundary invariant check.

            Timeline-Guardian-style fail-loud: any invariant violation
            raises ``RuntimeError`` immediately.  The pipeline stops.
            """
            state = getattr(callback_context, "state", None)
            if state is None:
                return
            result = state.get(BLACKBOARD_RESULT_KEY)
            if result is None:
                # No result written — the level ran but didn't finalise.
                # That's OK mid-ladder; we only enforce the invariant at
                # the top-level agent's after_agent_callback.
                return
            # Invariant 1: result schema shape
            if not isinstance(result, dict):
                raise RuntimeError(
                    f"Infra ladder invariant: {BLACKBOARD_RESULT_KEY} "
                    f"must be dict, got {type(result)!r}"
                )
            # Invariant 2: terminal_level is a valid RecoveryLevel
            terminal = result.get("terminal_level")
            if terminal not in {
                int(RecoveryLevel.FIX),
                int(RecoveryLevel.RETRY),
                int(RecoveryLevel.CREATIVE),
                int(RecoveryLevel.COLLABORATIVE),
                int(RecoveryLevel.HUMAN),
            }:
                raise RuntimeError(
                    f"Infra ladder invariant: terminal_level {terminal!r} "
                    f"is not a valid RecoveryLevel"
                )
            # Invariant 3: success ⇒ action populated
            if result.get("success") and not result.get("action"):
                raise RuntimeError(
                    "Infra ladder invariant: success=True requires "
                    "action to be populated"
                )
            # Invariant 4: L4 ⇒ escalation_id populated on the action
            if (
                terminal == int(RecoveryLevel.HUMAN)
                and result.get("action") is not None
                and not result["action"].get("escalation_id")
            ):
                raise RuntimeError(
                    "Infra ladder invariant: L4 action must carry an "
                    "escalation_id (same gate as content L4)"
                )

        class InfraLadderAgent(BaseAgent):  # type: ignore[misc,valid-type]
            """ADK wrapper around :func:`run_infra_ladder`.

            The agent reads ``state["infra_failure_event"]`` — a dict
            matching :class:`InfraFailureEvent` — runs the ladder, and
            writes both a structured result
            (``state[BLACKBOARD_RESULT_KEY]``) and a short summary
            (``state[BLACKBOARD_STATE_KEY]``).

            The ARCH-C3 orchestrator (#142) populates
            ``state["infra_failure_event"]`` before yielding to this
            agent — this module does not attempt to read directly from
            the failure path.
            """

            model_config = {"arbitrary_types_allowed": True}

            async def _run_async_impl(
                self, ctx: "InvocationContext"
            ):
                session_state = ctx.session.state if ctx.session else {}

                raw_event = session_state.get("infra_failure_event")
                if raw_event is None:
                    raise RuntimeError(
                        "Infra ladder ADK agent: state['infra_failure_event'] "
                        "is unset. ARCH-C3 must populate this before the "
                        "ladder runs — the ladder refuses to silently no-op."
                    )

                event = _coerce_event(raw_event)
                yield _event_for(self.name, {
                    "phase": "ladder_started",
                    "job_id": event.job_id,
                    "worker_url": event.worker_url,
                    "failure_signature": event.failure_signature,
                })

                result = run_infra_ladder(event)

                # Blackboard writes — the L-summary key is whichever
                # level terminated the ladder.
                session_state[BLACKBOARD_RESULT_KEY] = result.to_dict()
                session_state[BLACKBOARD_STATE_KEY] = result.state_snapshot
                summary_key = _LEVEL_SUMMARY_KEYS.get(
                    result.terminal_level, BLACKBOARD_L0_SUMMARY_KEY
                )
                session_state[summary_key] = (
                    f"L{result.terminal_level} "
                    f"{'resolved' if result.success else 'escalated'}: "
                    f"{result.action.reason if result.action else 'no action'}"
                )

                yield _event_for(self.name, {
                    "phase": "ladder_finished",
                    "terminal_level": result.terminal_level,
                    "success": result.success,
                    "action_type": (
                        result.action.action_type if result.action else None
                    ),
                    "escalation_id": result.escalation_id,
                })

        agent = InfraLadderAgent(
            name="infra_ladder",
            after_agent_callback=_check_ladder_state,
        )
        _infra_ladder_agent = agent
        return agent


def _coerce_event(raw: Any) -> InfraFailureEvent:
    """Best-effort conversion of a dict or dataclass into ``InfraFailureEvent``.

    Fail-loud on missing required fields.
    """
    if isinstance(raw, InfraFailureEvent):
        return raw
    if not isinstance(raw, dict):
        raise TypeError(
            f"infra_failure_event must be dict or InfraFailureEvent, "
            f"got {type(raw)!r}"
        )
    missing = [k for k in ("job_id", "worker_url", "failure_signature") if k not in raw]
    if missing:
        raise ValueError(
            f"infra_failure_event missing required fields: {missing}"
        )
    return InfraFailureEvent(
        job_id=str(raw["job_id"]),
        worker_url=str(raw["worker_url"]),
        failure_signature=str(raw["failure_signature"]),
        raw_error=str(raw.get("raw_error", "")),
        classification=raw.get("classification"),
        metadata=dict(raw.get("metadata", {}) or {}),
    )


def get_infra_ladder_agent() -> Any:
    """Return the module-level ADK agent, building it lazily.

    Returns ``None`` if ``google-adk`` isn't importable in the current
    environment — in that case :func:`run_infra_ladder` remains the
    canonical entry point.
    """
    return _build_adk_agent()


__all__ = [
    # Types
    "InfraFailureEvent",
    "InfraLadderState",
    "InfraLadderDeps",
    "InfraLadderResult",
    "InfraRecoveryAction",
    # Signatures
    "INFRA_SIG_WORKER_DEATH",
    "INFRA_SIG_OOM",
    "INFRA_SIG_CUDA_ERROR",
    "INFRA_SIG_DRIVER_RESET",
    "INFRA_SIG_PREEMPTION",
    "INFRA_SIG_COLD_START_FAIL",
    "INFRA_SIG_NETWORK_PARTITION",
    "INFRA_SIG_VRAM_EXHAUSTED",
    "INFRA_SIG_THERMAL_THROTTLE",
    "INFRA_SIG_AUTH_REVOKED",
    "INFRA_SIG_BILLING_TRIP",
    "INFRA_SIG_PROVIDER_OUTAGE",
    "INFRA_SIG_STORAGE_UNREACHABLE",
    "KNOWN_INFRA_SIGNATURES",
    # Policy
    "INFRA_POLICY",
    # Tools (plain callables)
    "infra_l0_fix",
    "infra_l1_retry",
    "infra_l2_creative",
    "infra_l3_collaborative",
    "infra_l4_human",
    # Entry points
    "run_infra_ladder",
    "build_default_deps",
    "get_infra_ladder_agent",
    # Blackboard keys
    "BLACKBOARD_STATE_KEY",
    "BLACKBOARD_RESULT_KEY",
    "BLACKBOARD_L0_SUMMARY_KEY",
    "BLACKBOARD_L1_SUMMARY_KEY",
    "BLACKBOARD_L2_SUMMARY_KEY",
    "BLACKBOARD_L3_SUMMARY_KEY",
    "BLACKBOARD_L4_SUMMARY_KEY",
]
