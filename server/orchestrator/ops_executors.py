"""Executors for the ops / deployment-planner escalation actions.

PR-2 introduces four new :class:`EscalationAction` variants that let the
supervisor act on the fleet rather than the artifact when the root-cause
is infrastructural (see :mod:`orchestrator.escalation_menu` docstring):

* ``recycle_worker`` — destroy + reprovision a single degraded worker.
* ``provision_extra_worker`` — add capacity for a stage.
* ``wait_for_worker_recovery`` — cheapest ops action; just pause.
* ``freeze_batch_and_replan`` — halt in-flight work, let the orchestrator
  regenerate remaining scenes.

This module *executes* those actions against the real fleet machinery
(:class:`worker_provisioner.WorkerProvisioner`,
:class:`infra_agent.InfraAgent`, and the
:class:`orchestrator.production_orchestrator.ProductionOrchestrator`).

The executor surface is intentionally **dependency-injectable**: each
executor looks up its collaborators through three factory slots that
tests can override (``set_provisioner_factory``,
``set_infra_agent_factory``, ``set_orchestrator_factory``).  If a
collaborator is unavailable the executor returns a graceful ``{"ok":
False, "reason": ...}`` result instead of raising — the escalation path
must never crash because a recovery attempt itself failed.

All executors are synchronous and return a small ``ExecutorResult``
(just a ``TypedDict``-shaped ``dict``) so they can be:

* called from unit tests with stub collaborators;
* logged verbatim into :class:`critique.record.EscalationRef.reasoning`;
* JSON-serialised into dashboards / B2 checkpoints.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from orchestrator.escalation_menu import (
    EscalationAction,
    EscalationActionError,
    OPS_ACTION_NAMES,
    OPS_VALID_ROLES,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dependency-injection factories
# ---------------------------------------------------------------------------

_ProvisionerFactory = Callable[[], Any]
_InfraAgentFactory = Callable[[], Any]
_OrchestratorFactory = Callable[[], Any]

_provisioner_factory: Optional[_ProvisionerFactory] = None
_infra_agent_factory: Optional[_InfraAgentFactory] = None
_orchestrator_factory: Optional[_OrchestratorFactory] = None


def set_provisioner_factory(factory: Optional[_ProvisionerFactory]) -> None:
    """Override the provisioner resolver (tests inject a stub)."""
    global _provisioner_factory
    _provisioner_factory = factory


def set_infra_agent_factory(factory: Optional[_InfraAgentFactory]) -> None:
    """Override the infra-agent resolver (tests inject a stub)."""
    global _infra_agent_factory
    _infra_agent_factory = factory


def set_orchestrator_factory(factory: Optional[_OrchestratorFactory]) -> None:
    """Override the production-orchestrator resolver (tests inject a stub)."""
    global _orchestrator_factory
    _orchestrator_factory = factory


def _resolve_provisioner() -> Optional[Any]:
    if _provisioner_factory is not None:
        try:
            return _provisioner_factory()
        except Exception as exc:  # pragma: no cover — test stubs never raise
            logger.warning("provisioner_factory raised: %s", exc)
            return None
    try:
        from worker_provisioner import get_provisioner  # type: ignore
        return get_provisioner()
    except Exception as exc:
        logger.debug("worker_provisioner unavailable: %s", exc)
        return None


def _resolve_infra_agent() -> Optional[Any]:
    if _infra_agent_factory is not None:
        try:
            return _infra_agent_factory()
        except Exception as exc:  # pragma: no cover
            logger.warning("infra_agent_factory raised: %s", exc)
            return None
    try:
        from infra_agent import get_infra_agent  # type: ignore
        return get_infra_agent()
    except Exception as exc:
        logger.debug("infra_agent unavailable: %s", exc)
        return None


def _resolve_orchestrator() -> Optional[Any]:
    if _orchestrator_factory is not None:
        try:
            return _orchestrator_factory()
        except Exception as exc:  # pragma: no cover
            logger.warning("orchestrator_factory raised: %s", exc)
            return None
    return None


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

def _result(
    ok: bool,
    action: str,
    *,
    detail: str = "",
    **extras: Any,
) -> dict[str, Any]:
    """Uniform shape for every executor.

    Returning a ``dict`` (rather than a dataclass) keeps the hot-path
    dependency-free and lets executor output drop straight into
    :class:`critique.record.EscalationRef` without adapters.
    """
    out: dict[str, Any] = {
        "ok": ok,
        "action": action,
        "detail": detail,
        "timestamp": time.time(),
    }
    out.update(extras)
    return out


# ---------------------------------------------------------------------------
# Individual executors
# ---------------------------------------------------------------------------

def execute_wait_for_worker_recovery(
    worker_url: str,
    timeout_sec: float,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Block up to ``timeout_sec`` waiting for a worker to become healthy.

    ``sleeper`` is injectable for tests so they don't actually sleep.

    This executor consults ``infra_agent`` repeatedly via its
    ``get_healthy_workers()`` snapshot (duck-typed).  If the infra agent
    isn't wired up we degrade to a simple ``time.sleep`` so the action
    remains observable in logs.
    """
    deadline = time.time() + max(0.0, float(timeout_sec))
    infra = _resolve_infra_agent()
    poll_interval = min(5.0, max(0.5, timeout_sec / 20.0))
    attempts = 0

    while time.time() < deadline:
        attempts += 1
        if infra is not None:
            get_healthy = getattr(infra, "get_healthy_workers", None)
            if callable(get_healthy):
                try:
                    healthy = list(get_healthy() or [])
                except Exception as exc:
                    logger.debug("get_healthy_workers raised: %s", exc)
                    healthy = []
                if worker_url in healthy:
                    return _result(
                        True,
                        "wait_for_worker_recovery",
                        detail=f"worker recovered after {attempts} poll(s)",
                        worker_url=worker_url,
                        attempts=attempts,
                    )
        sleeper(poll_interval)

    return _result(
        False,
        "wait_for_worker_recovery",
        detail=f"worker did not recover within {timeout_sec:.1f}s",
        worker_url=worker_url,
        attempts=attempts,
    )


def execute_recycle_worker(worker_url: str, reason: str) -> dict[str, Any]:
    """Destroy + reprovision a single worker.

    The real ``WorkerProvisioner`` does not yet expose a
    ``recycle_worker`` primitive; we look it up via ``getattr`` so a
    follow-up can add it without churning this executor.  If the
    provisioner is missing or the method isn't implemented we return a
    graceful failure result — the supervisor loop will record the
    outcome and can escalate further.
    """
    provisioner = _resolve_provisioner()
    infra = _resolve_infra_agent()
    if provisioner is None:
        return _result(
            False,
            "recycle_worker",
            detail="provisioner unavailable",
            worker_url=worker_url,
            reason=reason,
        )

    # Prefer a native recycle API if the provisioner exposes one.
    recycle = getattr(provisioner, "recycle_worker", None)
    if callable(recycle):
        try:
            info = recycle(worker_url, reason=reason)
        except Exception as exc:
            return _result(
                False,
                "recycle_worker",
                detail=f"recycle_worker raised: {exc}",
                worker_url=worker_url,
                reason=reason,
            )
        if infra is not None:
            _best_effort_remove(infra, worker_url)
        return _result(
            True,
            "recycle_worker",
            detail="recycle_worker returned",
            worker_url=worker_url,
            reason=reason,
            provisioner_info=_make_jsonable(info),
        )

    # Fallback: best-effort. Tell infra_agent the worker is out of
    # rotation; the provisioner's autonomous recovery loop can then
    # re-provision at its next tick.  This intentionally does NOT call
    # ``cleanup()`` — that would tear down ALL workers.
    removed = _best_effort_remove(infra, worker_url) if infra is not None else False
    return _result(
        False,
        "recycle_worker",
        detail=(
            "provisioner has no recycle_worker(); removed from infra_agent "
            "rotation (re-provision must be triggered elsewhere)"
        ),
        worker_url=worker_url,
        reason=reason,
        infra_removed=removed,
    )


def execute_provision_extra_worker(
    role: str,
    count: int,
) -> dict[str, Any]:
    """Add ``count`` extra workers of the given ``role`` to the fleet.

    Like :func:`execute_recycle_worker`, we look the method up via
    ``getattr`` so tests can stub it and live code can add it later
    without churn here.
    """
    if role not in OPS_VALID_ROLES:
        return _result(
            False,
            "provision_extra_worker",
            detail=f"invalid role {role!r}; expected one of {OPS_VALID_ROLES}",
            role=role,
            count=count,
        )
    provisioner = _resolve_provisioner()
    if provisioner is None:
        return _result(
            False,
            "provision_extra_worker",
            detail="provisioner unavailable",
            role=role,
            count=count,
        )

    add = getattr(provisioner, "provision_extra_workers", None) or getattr(
        provisioner, "provision_extra_worker", None
    )
    if not callable(add):
        return _result(
            False,
            "provision_extra_worker",
            detail="provisioner has no provision_extra_worker(s)() primitive",
            role=role,
            count=count,
        )

    try:
        info = add(role=role, count=count)
    except TypeError:
        # Older signature takes positional args.
        try:
            info = add(role, count)
        except Exception as exc:
            return _result(
                False,
                "provision_extra_worker",
                detail=f"provision_extra_worker raised: {exc}",
                role=role,
                count=count,
            )
    except Exception as exc:
        return _result(
            False,
            "provision_extra_worker",
            detail=f"provision_extra_worker raised: {exc}",
            role=role,
            count=count,
        )

    return _result(
        True,
        "provision_extra_worker",
        detail=f"requested {count} extra {role} worker(s)",
        role=role,
        count=count,
        provisioner_info=_make_jsonable(info),
    )


def execute_freeze_batch_and_replan(reason: str) -> dict[str, Any]:
    """Halt in-flight batch work and ask the orchestrator to replan.

    The live pipeline's pause surface is :meth:`infra_agent.InfraAgent._pause`,
    exposed via the public ``resume``/``is_paused`` pair.  We call the
    internal pause helper (which IS public in spirit — the rest of the
    module uses it) and leave resumption to the orchestrator's replan
    cycle.
    """
    infra = _resolve_infra_agent()
    paused = False
    pause_reason = reason

    if infra is not None:
        # Prefer the public API if one exists.
        pause_fn = (
            getattr(infra, "pause", None)
            or getattr(infra, "_pause", None)
        )
        if callable(pause_fn):
            try:
                pause_fn(reason)
                paused = True
            except Exception as exc:
                logger.warning("infra.pause raised: %s", exc)
                pause_reason = f"{reason} (pause failed: {exc})"

    orchestrator = _resolve_orchestrator()
    replan_requested = False
    if orchestrator is not None:
        replan = getattr(orchestrator, "request_replan", None) or getattr(
            orchestrator, "replan", None
        )
        if callable(replan):
            try:
                replan(reason=reason)
                replan_requested = True
            except TypeError:
                try:
                    replan(reason)
                    replan_requested = True
                except Exception as exc:
                    logger.warning("orchestrator.replan raised: %s", exc)
            except Exception as exc:
                logger.warning("orchestrator.replan raised: %s", exc)

    return _result(
        paused or replan_requested,
        "freeze_batch_and_replan",
        detail=(
            f"paused={paused} replan_requested={replan_requested} "
            f"reason={pause_reason!r}"
        ),
        reason=reason,
        paused=paused,
        replan_requested=replan_requested,
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def execute_ops_action(action: EscalationAction) -> dict[str, Any]:
    """Dispatch any ops :class:`EscalationAction` to its executor.

    Raises :class:`EscalationActionError` if ``action.action`` is not one
    of the four ops actions — callers should route creative actions via
    their existing executors.
    """
    if action.action not in OPS_ACTION_NAMES:
        raise EscalationActionError(
            f"execute_ops_action: {action.action!r} is not an ops action; "
            f"expected one of {OPS_ACTION_NAMES}"
        )

    if action.action == "wait_for_worker_recovery":
        if action.worker_url is None or action.timeout_sec is None:
            return _result(
                False,
                action.action,
                detail="missing worker_url or timeout_sec",
            )
        return execute_wait_for_worker_recovery(
            action.worker_url, action.timeout_sec
        )
    if action.action == "recycle_worker":
        if action.worker_url is None or action.reason is None:
            return _result(
                False,
                action.action,
                detail="missing worker_url or reason",
            )
        return execute_recycle_worker(action.worker_url, action.reason)
    if action.action == "provision_extra_worker":
        if action.role is None or action.count is None:
            return _result(
                False,
                action.action,
                detail="missing role or count",
            )
        return execute_provision_extra_worker(action.role, action.count)
    if action.action == "freeze_batch_and_replan":
        if action.reason is None:
            return _result(
                False,
                action.action,
                detail="missing reason",
            )
        return execute_freeze_batch_and_replan(action.reason)

    # Unreachable under the OPS_ACTION_NAMES guard above; kept for mypy
    # exhaustiveness.
    raise EscalationActionError(
        f"execute_ops_action: no executor for {action.action!r}"
    )  # pragma: no cover


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _best_effort_remove(infra: Any, worker_url: str) -> bool:
    """Tell ``infra_agent`` the worker is out of rotation, best-effort."""
    remove = getattr(infra, "remove_worker", None)
    if not callable(remove):
        return False
    try:
        remove(worker_url)
    except Exception as exc:
        logger.debug("infra.remove_worker(%s) raised: %s", worker_url, exc)
        return False
    return True


def _make_jsonable(value: Any) -> Any:
    """Best-effort coerce a return value into something JSON-safe.

    Executors call third-party provisioner / infra methods whose return
    types we can't guarantee; we only care that the ``detail`` dict
    survives ``json.dumps``.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_make_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _make_jsonable(v) for k, v in value.items()}
    return repr(value)


__all__ = [
    "execute_ops_action",
    "execute_recycle_worker",
    "execute_provision_extra_worker",
    "execute_wait_for_worker_recovery",
    "execute_freeze_batch_and_replan",
    "set_provisioner_factory",
    "set_infra_agent_factory",
    "set_orchestrator_factory",
]
