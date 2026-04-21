"""Component 10 — tactical recovery / escalation payload builders.

These are the recovery surface the production SubAgent calls when an
artifact fails QA or a launch raises. The concrete recovery *logic*
(re-launching via the task pool, regenerating the concept for
``fix_scene``, persisting a skip marker) ships with component 12, and
the escalation delegation ships with component 13.

This module provides the ``@tool``-decorated payload builders the
SubAgent can already call today:

* :func:`retry_scene` — records a retry request and increments the
  per-scene retry counter in the returned envelope.
* :func:`fix_scene` — records a fix request (prompt-level).
* :func:`skip_scene` — marks the scene as degraded.
* :func:`request_escalation` — builds the structured escalation
  payload the parent orchestrator forwards to the ``escalation``
  SubAgent.

Each tool returns a dict the SubAgent writes to
``production_report.json`` / ``escalation_requests.json``. Component
12/13 replace the in-module ledger with a richer implementation, but
the *tool signature* is preserved so the production SubAgent
trajectory is stable today.

Budgets (AGENTS.md invariants):

* ``RETRY_BUDGET = 2`` retries per scene.
* ``FIX_BUDGET = 1`` fix per scene.
* After either budget is exhausted the SubAgent MUST escalate.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from strands import tool

logger = logging.getLogger(__name__)


#: Max number of ``retry_scene`` invocations permitted per ``scene_id``.
RETRY_BUDGET: int = 2

#: Max number of ``fix_scene`` invocations permitted per ``scene_id``.
FIX_BUDGET: int = 1

_RECOVERY_ACTIONS: frozenset[str] = frozenset({"retry", "fix", "skip", "escalate"})


class RecoveryBudgetExhausted(RuntimeError):
    """Raised when a recovery call exceeds the per-scene budget.

    The SubAgent must either escalate or mark the scene skipped once
    this exception is raised — continuing to call ``retry_scene`` /
    ``fix_scene`` past budget is a prompt-drift bug.
    """


# ---------------------------------------------------------------------------
# Thread-safe per-scene recovery ledger.
# ---------------------------------------------------------------------------


class _RecoveryLedger:
    """Per-scene retry / fix counters. Shared across the SubAgent run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._retries: dict[str, int] = {}
        self._fixes: dict[str, int] = {}
        self._skips: dict[str, dict[str, Any]] = {}

    def increment_retry(self, scene_id: str) -> int:
        with self._lock:
            current = self._retries.get(scene_id, 0) + 1
            self._retries[scene_id] = current
            return current

    def increment_fix(self, scene_id: str) -> int:
        with self._lock:
            current = self._fixes.get(scene_id, 0) + 1
            self._fixes[scene_id] = current
            return current

    def mark_skipped(self, scene_id: str, reason: str) -> dict[str, Any]:
        with self._lock:
            entry = {"scene_id": scene_id, "reason": reason, "marked_at": time.time()}
            self._skips[scene_id] = entry
            return dict(entry)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "retries": dict(self._retries),
                "fixes": dict(self._fixes),
                "skips": {k: dict(v) for k, v in self._skips.items()},
            }

    def reset(self) -> None:
        with self._lock:
            self._retries.clear()
            self._fixes.clear()
            self._skips.clear()


_LEDGER: _RecoveryLedger = _RecoveryLedger()


def get_recovery_ledger() -> _RecoveryLedger:
    """Return the process-wide recovery ledger.

    Component 14 wires a dedicated ledger per DeepAgent run via
    :func:`set_recovery_ledger`; the module-level default exists so
    the SubAgent can be invoked end-to-end from tests without extra
    bootstrap.
    """
    return _LEDGER


def set_recovery_ledger(ledger: _RecoveryLedger | None) -> None:
    """Install a fresh recovery ledger (or reset to the default).

    Args:
        ledger: New ledger instance. Pass ``None`` to restore the
            module-level default and clear its state.
    """
    global _LEDGER
    if ledger is None:
        _LEDGER = _RecoveryLedger()
    else:
        _LEDGER = ledger


# ---------------------------------------------------------------------------
# @tool surface
# ---------------------------------------------------------------------------


@tool
def retry_scene(scene_id: str, reason: str) -> dict[str, Any]:
    """Record a retry request for ``scene_id``; enforces :data:`RETRY_BUDGET`.

    The tool does **not** re-launch the worker — it increments the
    per-scene retry counter and returns a structured envelope that
    component 12's orchestrator maps back to a fresh
    ``launch_visual_production`` call with ``revision = retry_count + 1``.

    Args:
        scene_id: Scene whose render should be retried.
        reason: Diagnostic string explaining why the retry is
            warranted (e.g. ``"worker_500"`` or ``"transient_timeout"``).

    Returns:
        Dict with ``action="retry"``, ``scene_id``, ``reason``,
        ``retry_count`` (post-increment), and ``next_revision``.

    Raises:
        ValueError: On empty ``scene_id`` / ``reason``.
        RecoveryBudgetExhausted: When the retry budget is already
            used up. The SubAgent must escalate.
    """
    if not scene_id:
        raise ValueError("retry_scene requires a non-empty scene_id")
    if not reason:
        raise ValueError("retry_scene requires a non-empty reason")

    ledger = get_recovery_ledger()
    current_snapshot = ledger.snapshot()
    already = current_snapshot["retries"].get(scene_id, 0)
    if already >= RETRY_BUDGET:
        raise RecoveryBudgetExhausted(
            f"retry_scene called for {scene_id} after {already} retries "
            f"(budget {RETRY_BUDGET}); SubAgent must escalate"
        )

    retry_count = ledger.increment_retry(scene_id)
    next_revision = retry_count + 1
    logger.debug(
        "scene_id=<%s>, reason=<%s>, retry_count=<%d> | retry recorded",
        scene_id,
        reason,
        retry_count,
    )
    return {
        "action": "retry",
        "scene_id": scene_id,
        "reason": reason,
        "retry_count": retry_count,
        "next_revision": next_revision,
        "budget": RETRY_BUDGET,
    }


@tool
def fix_scene(scene_id: str, reason: str) -> dict[str, Any]:
    """Record a fix request (prompt-level) for ``scene_id``.

    Component 12 will re-run the visual-concepter for the scene using
    ``reason`` as feedback and re-launch production with the new
    prompt. This tool merely records the fix and enforces
    :data:`FIX_BUDGET`.

    Args:
        scene_id: Scene whose prompt should be regenerated.
        reason: Diagnostic explaining why a prompt-level rewrite is
            needed (e.g. ``"style_drift"`` or ``"subject_missing"``).

    Returns:
        Dict with ``action="fix"``, ``scene_id``, ``reason``,
        ``fix_count`` (post-increment), and ``next_revision``
        (``retry_count + fix_count + 1``).

    Raises:
        ValueError: On empty ``scene_id`` / ``reason``.
        RecoveryBudgetExhausted: When the fix budget is already used
            up. The SubAgent must escalate or skip.
    """
    if not scene_id:
        raise ValueError("fix_scene requires a non-empty scene_id")
    if not reason:
        raise ValueError("fix_scene requires a non-empty reason")

    ledger = get_recovery_ledger()
    snapshot = ledger.snapshot()
    already = snapshot["fixes"].get(scene_id, 0)
    if already >= FIX_BUDGET:
        raise RecoveryBudgetExhausted(
            f"fix_scene called for {scene_id} after {already} fixes "
            f"(budget {FIX_BUDGET}); SubAgent must skip or escalate"
        )

    fix_count = ledger.increment_fix(scene_id)
    retry_count = snapshot["retries"].get(scene_id, 0)
    next_revision = retry_count + fix_count + 1
    logger.debug(
        "scene_id=<%s>, reason=<%s>, fix_count=<%d> | fix recorded",
        scene_id,
        reason,
        fix_count,
    )
    return {
        "action": "fix",
        "scene_id": scene_id,
        "reason": reason,
        "fix_count": fix_count,
        "next_revision": next_revision,
        "budget": FIX_BUDGET,
    }


@tool
def skip_scene(scene_id: str, reason: str) -> dict[str, Any]:
    """Mark ``scene_id`` as degraded / skipped. No budget.

    The SubAgent calls this when retries and fixes are exhausted but
    the failure is localised (one scene) and the documentary can still
    ship with the scene marked as missing. Component 11's assembly
    handles the gap with a still-frame or blackout.

    Args:
        scene_id: Scene to skip.
        reason: Diagnostic explaining why the scene cannot be
            rendered.

    Returns:
        Dict with ``action="skip"``, ``scene_id``, ``reason``, and
        ``marked_at``.

    Raises:
        ValueError: On empty ``scene_id`` / ``reason``.
    """
    if not scene_id:
        raise ValueError("skip_scene requires a non-empty scene_id")
    if not reason:
        raise ValueError("skip_scene requires a non-empty reason")

    ledger = get_recovery_ledger()
    entry = ledger.mark_skipped(scene_id, reason)
    logger.debug(
        "scene_id=<%s>, reason=<%s> | scene marked skipped",
        scene_id,
        reason,
    )
    return {
        "action": "skip",
        **entry,
    }


@tool
def request_escalation(
    scene_id: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the escalation payload forwarded to the ``escalation`` SubAgent.

    This is the single recovery surface that transfers control out of
    the production SubAgent. The parent orchestrator inspects the
    returned payload and delegates to the ``escalation`` SubAgent via
    ``task(subagent_type="escalation", ...)``.

    Args:
        scene_id: Scene that triggered the escalation. Pass
            ``"_global"`` for pool-wide failures (e.g. all workers
            down).
        reason: Short diagnostic label (e.g.
            ``"retry_budget_exhausted"``, ``"fix_budget_exhausted"``,
            ``"worker_pool_degraded"``).
        evidence: Optional free-form dict with supporting context
            (last worker error, artifact QA issues, health snapshot).

    Returns:
        Dict with ``action="escalate"``, ``scene_id``, ``reason``,
        ``evidence`` (copied), ``ledger`` (full retry/fix/skip
        snapshot), and ``requested_at``.

    Raises:
        ValueError: On empty ``scene_id`` / ``reason``.
    """
    if not scene_id:
        raise ValueError("request_escalation requires a non-empty scene_id")
    if not reason:
        raise ValueError("request_escalation requires a non-empty reason")

    ledger = get_recovery_ledger()
    payload = {
        "action": "escalate",
        "scene_id": scene_id,
        "reason": reason,
        "evidence": dict(evidence) if evidence else {},
        "ledger": ledger.snapshot(),
        "requested_at": time.time(),
    }
    logger.info(
        "scene_id=<%s>, reason=<%s> | escalation requested", scene_id, reason
    )
    return payload


__all__ = [
    "FIX_BUDGET",
    "RETRY_BUDGET",
    "RecoveryBudgetExhausted",
    "fix_scene",
    "get_recovery_ledger",
    "request_escalation",
    "retry_scene",
    "set_recovery_ledger",
    "skip_scene",
]
