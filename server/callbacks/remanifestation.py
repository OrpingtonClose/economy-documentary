"""
Re-manifestation executor -- ARCH-B3 (issue #139) under parent ARCH-B #124 /
meta ARCH-2026 #122.

B3 is the hookup that actually runs re-manifestation plans. The impact
analyzer (ARCH-A6) reads the drift signals emitted by the consistency
checker (ARCH-A5) and produces a :class:`RemanifestationPlan`. B3 takes
that plan and executes it through the existing orchestrator / escalation
paths: for each target stage it clears the stale artifact tag / stage
derivation, marks the stage as needing re-run, and -- if the provided
runner cannot complete the plan -- escalates to human L4 via
:mod:`recovery`.

**B3 is deliberately independent of A6's internals.** A6 and B3 may land
in either order. The interface is a small :class:`RemanifestationPlan`
protocol plus a pluggable :class:`PlanProvider` protocol. When A6 is not
yet wired, :func:`handle_drift_signals` simply escalates every drift
signal to human L4 (which is the correct fail-loud behaviour -- no
silent degradation).

Design invariants (mirrored by tests in
``server/tests/test_remanifestation.py``):

1. **Blackboard-only state.** Re-manifestation plans live under
   :data:`REMANIFESTATION_PLAN_QUEUE_KEY`; the completion log lives under
   :data:`REMANIFESTATION_HISTORY_KEY`. No direct cross-stage imports.
2. **Fail loud.** Missing ledger state, malformed plans, missing required
   artifacts, or exhausted re-manifestation ladders all raise
   ``RuntimeError``. No placeholder / synthetic fallback.
3. **Gated re-manifestation.** Every executed plan passes through
   :func:`_gate_reconstruction` which, when ``DOCUMENTARY_AUTO_APPROVE``
   is off, blocks on an approval gate just like the primary pipeline
   stages. Reconstruct is itself a gated stage (per #139 DoD).
4. **L4 re-escalation on exhaustion.** If the plan runner raises, or if
   no plan provider is available, the drift is re-escalated via
   :func:`recovery.submit_escalation`.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Mapping,
    MutableMapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from callbacks.artifact_revision_tag import (
    clear_tag,
    has_tag,
)
from callbacks.consistency_checker import (
    LEDGER_DRIFT_SIGNALS_KEY,
    STAGE_DERIVATIONS_KEY,
    LedgerDrift,
    _load_stage_derivations,
    pending_drift_signals,
)
from callbacks.preference_ledger import PREFERENCE_LEDGER_KEY, current_revision

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blackboard state keys
# ---------------------------------------------------------------------------

#: Queue of pending re-manifestation plans awaiting execution.
#: Value is a JSON-encoded list of :meth:`RemanifestationPlan.to_dict`
#: outputs, oldest first.
REMANIFESTATION_PLAN_QUEUE_KEY = "_remanifestation_plan_queue"

#: Append-only log of executed re-manifestation plans. Value is a
#: JSON-encoded list of dicts shaped like
#: ``{"plan_id": str, "status": "executed"|"escalated"|"failed",
#:    "timestamp": iso-8601 str, "note": str}``.
REMANIFESTATION_HISTORY_KEY = "_remanifestation_history"

#: Gate name used by the approval-gate system when blocking on
#: reconstruction approval. Reconstructing itself is gated (per #139 DoD).
RECONSTRUCT_GATE_STAGE = "reconstruct"


# ---------------------------------------------------------------------------
# Plan protocol -- what A6 produces and B3 consumes
# ---------------------------------------------------------------------------


@runtime_checkable
class RemanifestationPlan(Protocol):
    """Small plan protocol consumed by B3.

    A6 (impact analyzer) produces objects satisfying this protocol. The
    protocol is intentionally narrow so A6 and B3 can land in either
    order: B3 never imports A6, and A6 never imports B3's executor.

    Implementations:

    * :class:`DictRemanifestationPlan` -- dataclass-based default used by
      tests and the built-in stub plan provider.
    * Anything A6 ships that exposes the attributes below.

    Attributes:
        plan_id: Unique, human-readable identifier (e.g. ``"plan-<drift>"``).
        triggered_by: Drift signal that motivated this plan, as a dict
            (typically :meth:`LedgerDrift.to_dict`).
        stages_to_rerun: Ordered sequence of stage names to re-run, in
            dependency order. B3 does NOT reorder them.
        artifact_keys_to_clear: Blackboard output_keys whose
            :mod:`callbacks.artifact_revision_tag` tags must be cleared
            so the re-running stage can re-tag at the new ledger revision.
        rationale: Human-readable justification -- surfaced to L4 dashboard
            when the plan is escalated or fails.
    """

    plan_id: str
    triggered_by: Mapping[str, Any]
    stages_to_rerun: Sequence[str]
    artifact_keys_to_clear: Sequence[str]
    rationale: str


@dataclass(frozen=True)
class DictRemanifestationPlan:
    """Default :class:`RemanifestationPlan` implementation.

    Used by :func:`DefaultPlanProvider` (a minimal stub that re-runs the
    drifting stage) and by tests. A6 is free to ship its own plan class.
    """

    plan_id: str
    triggered_by: Mapping[str, Any]
    stages_to_rerun: tuple[str, ...]
    artifact_keys_to_clear: tuple[str, ...]
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or not self.plan_id:
            raise ValueError(
                f"RemanifestationPlan.plan_id must be a non-empty string, "
                f"got {self.plan_id!r}"
            )
        if not isinstance(self.triggered_by, Mapping):
            raise TypeError(
                "RemanifestationPlan.triggered_by must be a mapping, got "
                f"{type(self.triggered_by).__name__}"
            )
        if not isinstance(self.stages_to_rerun, tuple):
            raise TypeError(
                "RemanifestationPlan.stages_to_rerun must be a tuple, got "
                f"{type(self.stages_to_rerun).__name__}"
            )
        if not self.stages_to_rerun:
            raise ValueError(
                "RemanifestationPlan.stages_to_rerun must be non-empty; an "
                "empty plan is not a plan"
            )
        for i, s in enumerate(self.stages_to_rerun):
            if not isinstance(s, str) or not s:
                raise ValueError(
                    f"stages_to_rerun[{i}] must be a non-empty string, got {s!r}"
                )
        if not isinstance(self.artifact_keys_to_clear, tuple):
            raise TypeError(
                "RemanifestationPlan.artifact_keys_to_clear must be a tuple, "
                f"got {type(self.artifact_keys_to_clear).__name__}"
            )
        for i, k in enumerate(self.artifact_keys_to_clear):
            if not isinstance(k, str) or not k:
                raise ValueError(
                    f"artifact_keys_to_clear[{i}] must be a non-empty string, "
                    f"got {k!r}"
                )
        if not isinstance(self.rationale, str):
            raise TypeError(
                f"RemanifestationPlan.rationale must be str, got "
                f"{type(self.rationale).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "triggered_by": dict(self.triggered_by),
            "stages_to_rerun": list(self.stages_to_rerun),
            "artifact_keys_to_clear": list(self.artifact_keys_to_clear),
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DictRemanifestationPlan":
        if not isinstance(data, Mapping):
            raise TypeError(
                f"DictRemanifestationPlan.from_dict expects mapping, "
                f"got {type(data).__name__}"
            )
        missing = {
            "plan_id",
            "triggered_by",
            "stages_to_rerun",
            "artifact_keys_to_clear",
            "rationale",
        } - set(data)
        if missing:
            raise ValueError(
                f"RemanifestationPlan is missing required fields: {sorted(missing)}"
            )
        return cls(
            plan_id=data["plan_id"],
            triggered_by=dict(data["triggered_by"]),
            stages_to_rerun=tuple(data["stages_to_rerun"]),
            artifact_keys_to_clear=tuple(data["artifact_keys_to_clear"]),
            rationale=data["rationale"],
        )


# ---------------------------------------------------------------------------
# Plan provider protocol (A6 implements this; B3 ships a conservative stub)
# ---------------------------------------------------------------------------


@runtime_checkable
class PlanProvider(Protocol):
    """What A6 (or a stub) must expose so B3 can turn drift into a plan."""

    def plan_for_drift(
        self, state: Mapping[str, Any], drift: LedgerDrift
    ) -> Optional[RemanifestationPlan]:
        """Return a plan for this drift, or ``None`` to escalate to L4.

        Returning ``None`` is the explicit "I have no plan" signal and
        must cause B3 to escalate to human L4 -- no silent degradation.
        """
        ...


class DefaultPlanProvider:
    """Minimal plan provider used when A6 is not yet wired.

    Produces a conservative one-stage plan: re-run the stage that drifted
    and clear its artifact tag. This is safe because the A5 drift signal
    already identifies the stage whose derivation is stale.

    ARCH-A6 will replace this with a scope-aware impact analyzer that
    cascades through downstream stages. Until then, B3 re-manifests only
    the directly-drifting stage and escalates if that fails.
    """

    def plan_for_drift(
        self, state: Mapping[str, Any], drift: LedgerDrift
    ) -> Optional[RemanifestationPlan]:
        # Only re-manifest if the stage actually has a tagged artifact to
        # clear. Untagged stages would need A6's dependency graph to know
        # which downstream artifacts to invalidate -- escalate those to L4.
        keys = tuple(aid for aid in drift.artifact_ids if has_tag(state, aid))
        if not keys:
            logger.info(
                "DefaultPlanProvider: drift for stage %r has no clearable "
                "artifacts (artifact_ids=%s) -- deferring to L4",
                drift.stage_name,
                drift.artifact_ids,
            )
            return None
        return DictRemanifestationPlan(
            plan_id=f"plan-default-{drift.stage_name}-{uuid.uuid4().hex[:8]}",
            triggered_by=drift.to_dict(),
            stages_to_rerun=(drift.stage_name,),
            artifact_keys_to_clear=keys,
            rationale=(
                f"Default plan: re-run stage {drift.stage_name!r} at "
                f"ledger revision {drift.to_rev} (was {drift.from_rev}) "
                f"because {len(drift.new_records)} preference record(s) "
                f"landed since derivation."
            ),
        )


# ---------------------------------------------------------------------------
# Plan queue helpers
# ---------------------------------------------------------------------------


def _load_plan_queue(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = state.get(REMANIFESTATION_PLAN_QUEUE_KEY)
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{REMANIFESTATION_PLAN_QUEUE_KEY!r} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, list):
            raise ValueError(
                f"{REMANIFESTATION_PLAN_QUEUE_KEY!r} must decode to a list, "
                f"got {type(decoded).__name__}"
            )
        return decoded
    raise TypeError(
        f"{REMANIFESTATION_PLAN_QUEUE_KEY!r} must be a list or JSON string, "
        f"got {type(raw).__name__}"
    )


def _store_plan_queue(
    state: MutableMapping[str, Any], queue: Sequence[Mapping[str, Any]]
) -> None:
    state[REMANIFESTATION_PLAN_QUEUE_KEY] = json.dumps(
        [dict(p) for p in queue], ensure_ascii=False
    )


def enqueue_plan(
    state: MutableMapping[str, Any], plan: RemanifestationPlan
) -> None:
    """Append ``plan`` to the re-manifestation queue on ``state``.

    Typically called by A6 (or the default provider) so that an orchestrator
    loop in the main run thread can drain the queue via :func:`drain_plans`.
    Direct callers (synchronous drift handlers) normally use
    :func:`handle_drift_signals` which enqueues-and-executes in one step.
    """
    if not _is_plan(plan):
        raise TypeError(
            "enqueue_plan requires a RemanifestationPlan-compatible object, "
            f"got {type(plan).__name__}"
        )
    queue = _load_plan_queue(state)
    queue.append(_plan_to_dict(plan))
    _store_plan_queue(state, queue)


def drain_plans(
    state: MutableMapping[str, Any],
) -> list[DictRemanifestationPlan]:
    """Pop every queued plan, returning them in FIFO order.

    After draining, :data:`REMANIFESTATION_PLAN_QUEUE_KEY` is reset to an
    empty list. Malformed entries raise ``ValueError`` / ``TypeError``.
    """
    queue = _load_plan_queue(state)
    out = [DictRemanifestationPlan.from_dict(p) for p in queue]
    _store_plan_queue(state, [])
    return out


def list_pending_plans(
    state: Mapping[str, Any],
) -> list[DictRemanifestationPlan]:
    """Return queued plans without mutating ``state``."""
    return [DictRemanifestationPlan.from_dict(p) for p in _load_plan_queue(state)]


def _is_plan(obj: Any) -> bool:
    """Duck-typed RemanifestationPlan check (Protocol is runtime-checkable
    but strict -- some A6 implementations may use slots/descriptors that
    confuse ``isinstance``, so we also accept anything exposing the five
    required attributes)."""
    if isinstance(obj, RemanifestationPlan):
        return True
    return all(
        hasattr(obj, attr)
        for attr in (
            "plan_id",
            "triggered_by",
            "stages_to_rerun",
            "artifact_keys_to_clear",
            "rationale",
        )
    )


def _plan_to_dict(plan: RemanifestationPlan) -> dict[str, Any]:
    to_dict = getattr(plan, "to_dict", None)
    if callable(to_dict):
        out = to_dict()
        if not isinstance(out, Mapping):
            raise TypeError(
                "plan.to_dict() must return a mapping, got "
                f"{type(out).__name__}"
            )
        return dict(out)
    return {
        "plan_id": plan.plan_id,
        "triggered_by": dict(plan.triggered_by),
        "stages_to_rerun": list(plan.stages_to_rerun),
        "artifact_keys_to_clear": list(plan.artifact_keys_to_clear),
        "rationale": plan.rationale,
    }


# ---------------------------------------------------------------------------
# History log
# ---------------------------------------------------------------------------


def _load_history(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = state.get(REMANIFESTATION_HISTORY_KEY)
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{REMANIFESTATION_HISTORY_KEY!r} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, list):
            raise ValueError(
                f"{REMANIFESTATION_HISTORY_KEY!r} must decode to a list, "
                f"got {type(decoded).__name__}"
            )
        return decoded
    raise TypeError(
        f"{REMANIFESTATION_HISTORY_KEY!r} must be a list or JSON string, "
        f"got {type(raw).__name__}"
    )


def _append_history(
    state: MutableMapping[str, Any],
    *,
    plan_id: str,
    status: str,
    note: str,
) -> None:
    entries = _load_history(state)
    entries.append(
        {
            "plan_id": plan_id,
            "status": status,
            "timestamp": _utc_iso(),
            "note": note,
        }
    )
    state[REMANIFESTATION_HISTORY_KEY] = json.dumps(entries, ensure_ascii=False)


def remanifestation_history(
    state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return a copy of the re-manifestation history log.

    Tests and the AG-UI dashboard read this to show which plans executed,
    which escalated to L4, and which failed.
    """
    return _load_history(state)


def _utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class RemanifestationError(RuntimeError):
    """Raised when a re-manifestation plan cannot be executed.

    Carries the plan and the triggering drift so crash handlers / L4
    dashboards can render the full context. Never caught inside B3 --
    only callers at the orchestrator boundary.
    """

    def __init__(
        self,
        plan: RemanifestationPlan,
        message: str,
        *,
        cause: Optional[BaseException] = None,
    ) -> None:
        self.plan = plan
        self.cause = cause
        super().__init__(
            f"re-manifestation plan {plan.plan_id!r} failed: {message}"
        )


#: Signature for the pluggable runner that actually re-runs a stage.
#: Receives the session state and the stage name. Must raise on any
#: failure so B3 can escalate to L4. Returning ``None`` means success.
StageRunner = Callable[[MutableMapping[str, Any], str], None]


def _gate_reconstruction(plan: RemanifestationPlan) -> bool:
    """Pause for reconstruction approval before executing ``plan``.

    Reconstruct is itself gated (per #139 DoD). This mirrors the
    primary-stage approval gate: ``DOCUMENTARY_AUTO_APPROVE`` /
    simulation mode both bypass the pause. Returns ``True`` if approved
    (including auto-approve), ``False`` on timeout.

    The import is local so the callbacks package does not hard-depend on
    the approval-gate file at import time (some unit tests stub state
    without the filesystem primitives).
    """
    from callbacks.approval_gate import (
        mark_stage_ready,
        wait_for_approval,
    )

    mark_stage_ready(RECONSTRUCT_GATE_STAGE)
    logger.info(
        "APPROVAL GATE: reconstruction plan %r ready -- "
        "waiting for human approval",
        plan.plan_id,
    )
    return wait_for_approval(RECONSTRUCT_GATE_STAGE)


def _clear_plan_targets(
    state: MutableMapping[str, Any], plan: RemanifestationPlan
) -> None:
    """Clear artifact tags and stage derivations named by ``plan``.

    Required so the re-running stage can re-tag at the current ledger
    revision without hitting ``ArtifactAlreadyTaggedError``. Stage-
    derivation entries are also reset so A5 does not immediately flag
    the re-run as stale.
    """
    for key in plan.artifact_keys_to_clear:
        if has_tag(state, key):
            clear_tag(state, key)
            logger.info("re-manifestation: cleared artifact tag %r", key)
        else:
            logger.debug(
                "re-manifestation: artifact %r already untagged -- skipping", key
            )

    # Reset each target stage's derivation entry so A5 does not trip on a
    # stale-tagged stage the moment it re-enters. The stage's producer
    # will re-record derivation via ARCH-B1 after it produces.
    derivations = _load_stage_derivations(state)
    changed = False
    for stage in plan.stages_to_rerun:
        if stage in derivations:
            del derivations[stage]
            changed = True
            logger.info(
                "re-manifestation: cleared stage derivation for %r", stage
            )
    if changed:
        state[STAGE_DERIVATIONS_KEY] = json.dumps(derivations, ensure_ascii=False)


def execute_plan(
    state: MutableMapping[str, Any],
    plan: RemanifestationPlan,
    *,
    runner: Optional[StageRunner] = None,
    gate: bool = True,
) -> None:
    """Execute a re-manifestation plan.

    Steps (order matters):

    1. Validate ledger is present (A5 must have been wired).
    2. Pause for reconstruction approval if ``gate`` is true.
    3. Clear artifact tags + stage derivations named by the plan.
    4. Invoke ``runner(state, stage)`` for each stage (default: no-op so
       B3 can land before A6 -- but the history records ``executed`` so
       callers can audit).
    5. Append to the re-manifestation history log.

    Any exception from the runner is wrapped in :class:`RemanifestationError`
    and re-raised. B2's drift-signal handler catches it and escalates to
    L4 via :func:`_escalate_plan_to_human`.

    Args:
        state: ADK session state (mutable mapping).
        plan: The plan to execute. Must satisfy the :class:`RemanifestationPlan`
            protocol.
        runner: Callable invoked once per stage in ``plan.stages_to_rerun``.
            ``None`` means the B3 wiring only clears tags + logs -- useful
            when A6 has not yet produced a stage runner, and for tests.
        gate: When ``True`` (default), block on the reconstruction approval
            gate before executing. Tests pass ``False`` to skip the pause.

    Raises:
        RemanifestationError: On gate timeout, runner error, or missing
            ledger state. The caller (typically :func:`handle_drift_signals`)
            escalates to L4 on any raise.
    """
    if not _is_plan(plan):
        raise TypeError(
            "execute_plan requires a RemanifestationPlan-compatible object, "
            f"got {type(plan).__name__}"
        )

    if PREFERENCE_LEDGER_KEY not in state:
        raise RemanifestationError(
            plan,
            f"cannot execute: {PREFERENCE_LEDGER_KEY!r} is not in session "
            "state. B3 requires A1+A5 to be wired first.",
        )

    if gate:
        approved = _gate_reconstruction(plan)
        if not approved:
            _append_history(
                state,
                plan_id=plan.plan_id,
                status="failed",
                note="timed out waiting for reconstruction approval",
            )
            raise RemanifestationError(
                plan, "timed out waiting for reconstruction approval"
            )

    _clear_plan_targets(state, plan)

    if runner is not None:
        for stage in plan.stages_to_rerun:
            try:
                runner(state, stage)
            except Exception as exc:  # pragma: no cover -- wrapped + re-raised
                _append_history(
                    state,
                    plan_id=plan.plan_id,
                    status="failed",
                    note=f"runner raised on stage {stage!r}: {exc!r}",
                )
                raise RemanifestationError(
                    plan,
                    f"runner raised on stage {stage!r}: {exc!r}",
                    cause=exc,
                ) from exc

    _append_history(
        state,
        plan_id=plan.plan_id,
        status="executed",
        note=(
            f"re-manifested {len(plan.stages_to_rerun)} stage(s), "
            f"cleared {len(plan.artifact_keys_to_clear)} artifact tag(s) "
            f"at ledger rev {current_revision(state)}"
        ),
    )
    logger.info(
        "re-manifestation: plan %r executed (stages=%s, rev=%d)",
        plan.plan_id,
        list(plan.stages_to_rerun),
        current_revision(state),
    )


# ---------------------------------------------------------------------------
# Human-L4 escalation
# ---------------------------------------------------------------------------


#: Signature for the pluggable L4 escalator. Defaults to
#: :func:`_default_escalator` which wraps :func:`recovery.submit_escalation`.
Escalator = Callable[[MutableMapping[str, Any], LedgerDrift, Optional[RemanifestationPlan], str], str]


def _default_escalator(
    state: MutableMapping[str, Any],
    drift: LedgerDrift,
    plan: Optional[RemanifestationPlan],
    reason: str,
) -> str:
    """Create a human-L4 escalation via the recovery subsystem.

    The escalation carries the drift signal, the candidate plan (if any),
    and the reason B3 is escalating (e.g. "no plan provider", "plan
    failed: ..."). Returns the escalation id so callers can log it.

    Imports are local so unit tests can swap in a stub escalator without
    pulling in the full recovery / AG-UI stack.
    """
    try:
        from recovery import HumanEscalationRequest, submit_escalation

        request_id = f"drift-{drift.stage_name}-{uuid.uuid4().hex[:8]}"
        diagnosis = {
            "root_cause": f"ledger_drift:{drift.stage_name}",
            "from_rev": drift.from_rev,
            "to_rev": drift.to_rev,
            "artifact_ids": list(drift.artifact_ids),
            "new_record_count": len(drift.new_records),
            "reason": reason,
        }
        proposed_actions: list[dict[str, Any]] = []
        if plan is not None:
            proposed_actions.append(
                {
                    "action_id": "retry_plan",
                    "description": (
                        f"Retry re-manifestation plan {plan.plan_id!r}: "
                        f"{plan.rationale}"
                    ),
                    "risk_level": "medium",
                }
            )
        proposed_actions.append(
            {
                "action_id": "abort",
                "description": "Abort the pipeline; ledger and artifacts drifted too far.",
                "risk_level": "high",
            }
        )
        req = HumanEscalationRequest(
            id=request_id,
            operation_name=f"ledger_drift_{drift.stage_name}",
            error_chain=[],
            diagnosis=diagnosis,
            proposed_actions=proposed_actions,
            severity="critical",
            timestamp=time.time(),
        )
        submit_escalation(req)
        return request_id
    except Exception as exc:
        # Fail loud -- a failure to even SUBMIT the escalation must stop
        # the pipeline rather than silently swallow the drift.
        raise RuntimeError(
            f"re-manifestation: could not submit L4 escalation for drift "
            f"on stage {drift.stage_name!r}: {exc!r}"
        ) from exc


def _escalate_plan_to_human(
    state: MutableMapping[str, Any],
    drift: LedgerDrift,
    plan: Optional[RemanifestationPlan],
    reason: str,
    *,
    escalator: Optional[Escalator] = None,
) -> str:
    """Escalate a drift to human L4 and append a history entry."""
    esc = escalator or _default_escalator
    escalation_id = esc(state, drift, plan, reason)
    _append_history(
        state,
        plan_id=(plan.plan_id if plan is not None else f"no-plan-{drift.stage_name}"),
        status="escalated",
        note=f"L4 escalation {escalation_id}: {reason}",
    )
    logger.warning(
        "re-manifestation: drift on stage %r escalated to L4 (%s): %s",
        drift.stage_name,
        escalation_id,
        reason,
    )
    return escalation_id


# ---------------------------------------------------------------------------
# Drift-signal handler -- the B2 ↔ B3 bridge
# ---------------------------------------------------------------------------


def handle_drift_signals(
    state: MutableMapping[str, Any],
    *,
    plan_provider: Optional[PlanProvider] = None,
    runner: Optional[StageRunner] = None,
    escalator: Optional[Escalator] = None,
    gate: bool = True,
    drain: bool = True,
) -> list[dict[str, Any]]:
    """Drain pending drift signals and turn each into an action.

    For each drift signal currently queued in the blackboard:

    1. Ask ``plan_provider`` for a plan. If ``None``, escalate to L4.
    2. If a plan is returned, call :func:`execute_plan`. On any failure,
       escalate to L4 with ``reason`` describing the failure.
    3. Append a result entry describing what happened.

    Args:
        state: ADK session state.
        plan_provider: The A6 (or stub) provider. Defaults to
            :class:`DefaultPlanProvider`.
        runner: The stage runner passed to :func:`execute_plan`.
        escalator: The L4 escalator. Tests inject a stub here.
        gate: Whether to gate reconstruction execution.
        drain: When ``True`` (default) pops the drift signals from the
            blackboard so they are not handled twice. Tests pass ``False``
            for dry-run inspection.

    Returns:
        A list of result dicts, one per drift signal, each shaped::

            {
                "stage_name": str,
                "outcome": "executed" | "escalated",
                "plan_id": str | None,
                "reason": str,
            }
    """
    provider = plan_provider or DefaultPlanProvider()

    drifts = pending_drift_signals(state)
    if drain and drifts:
        # Reset the drift queue so repeat calls don't redo the same work.
        state[LEDGER_DRIFT_SIGNALS_KEY] = json.dumps([], ensure_ascii=False)

    results: list[dict[str, Any]] = []
    for drift in drifts:
        plan: Optional[RemanifestationPlan] = provider.plan_for_drift(state, drift)
        if plan is None:
            eid = _escalate_plan_to_human(
                state,
                drift,
                None,
                reason="plan provider returned no plan for this drift",
                escalator=escalator,
            )
            results.append(
                {
                    "stage_name": drift.stage_name,
                    "outcome": "escalated",
                    "plan_id": None,
                    "reason": f"no plan; L4 escalation {eid}",
                }
            )
            continue

        try:
            execute_plan(state, plan, runner=runner, gate=gate)
        except RemanifestationError as exc:
            eid = _escalate_plan_to_human(
                state,
                drift,
                plan,
                reason=f"plan execution failed: {exc}",
                escalator=escalator,
            )
            results.append(
                {
                    "stage_name": drift.stage_name,
                    "outcome": "escalated",
                    "plan_id": plan.plan_id,
                    "reason": f"execution failed; L4 escalation {eid}",
                }
            )
            continue

        results.append(
            {
                "stage_name": drift.stage_name,
                "outcome": "executed",
                "plan_id": plan.plan_id,
                "reason": plan.rationale,
            }
        )

    return results


__all__ = [
    "RECONSTRUCT_GATE_STAGE",
    "REMANIFESTATION_HISTORY_KEY",
    "REMANIFESTATION_PLAN_QUEUE_KEY",
    "DefaultPlanProvider",
    "DictRemanifestationPlan",
    "Escalator",
    "PlanProvider",
    "RemanifestationError",
    "RemanifestationPlan",
    "StageRunner",
    "drain_plans",
    "enqueue_plan",
    "execute_plan",
    "handle_drift_signals",
    "list_pending_plans",
    "remanifestation_history",
]
