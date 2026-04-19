"""
Consistency gate -- ARCH-B2 (issue #138) under parent ARCH-B #124 / meta
ARCH-2026 #122.

B2 wires the A5 consistency checker
(:mod:`callbacks.consistency_checker`) into every pipeline checkpoint that
can witness ledger drift:

* **Stage boundaries** -- every ADK agent's ``after_agent_callback`` runs
  :func:`consistency_checker.check_consistency`. Mirrors the Timeline
  Guardian pattern (#84, ARCH-F series).
* **Stage entries** -- every ADK agent's ``before_agent_callback`` runs
  the same check so a stage starting with a stale
  ``ledger_revision_at_birth`` triggers drift detection BEFORE doing work.
* **Tool calls** -- every ADK ``before_tool_callback`` runs
  :func:`consistency_checker.before_tool_consistency_check`. Tool calls
  that mutate artifacts are first-class drift witnesses (per issue #138).
* **Approval-gate polls** -- :func:`gate_poll_consistency_check` is
  called from inside :mod:`callbacks.approval_gate`'s poll loop so a
  drift observed while humans are reviewing is handled immediately.

On drift, B2 delegates to :mod:`callbacks.remanifestation` (ARCH-A6).
:func:`remanifestation.handle_drift` drains the drift queue, calls the
A6 impact analyser / planner / validator / executor for each signal,
and re-escalates to human L4 on exhaustion. **No silent degradation.**

Design invariants (mirrored by tests in
``server/tests/test_consistency_gate.py``):

1. **Composition only -- never replace existing callbacks.** The factory
   helpers return composed callbacks that invoke both the original
   callback and the B2 hook, in that order. The original callback runs
   first so stage postconditions / contract validation stay authoritative;
   B2 runs after so it witnesses the post-stage state.
2. **Never short-circuit.** A drift is a signal, not an error (per A5
   invariant 4). All B2 callbacks return whatever the original callback
   returned; the consistency check runs "alongside" the pipeline.
3. **Fail loud on invariant violations.** Missing ledger state, revision
   decrease, or executor exhaustion all raise -- the B3 handler itself
   escalates to L4 rather than swallowing the error.
4. **Idempotent wiring.** :func:`wire_consistency_checks_into_agents` can
   be called twice without double-chaining (each agent is tagged with a
   sentinel attribute once wired).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional

from callbacks.consistency_checker import (
    LedgerDrift,
    after_agent_consistency_check,
    before_tool_consistency_check,
    check_consistency_at_gate,
    pending_drift_signals,
)
from callbacks.remanifestation import (
    DriftHandlingReceipt,
    StepExecutor,
    handle_drift,
)

logger = logging.getLogger(__name__)


#: Sentinel attribute set on an agent whose callbacks have already been
#: chained with the B2 consistency checks. Prevents double-wiring when
#: the pipeline module is re-imported (e.g. during testing or a hot
#: reload).
_WIRED_ATTR = "_arch_b2_consistency_wired"


# ---------------------------------------------------------------------------
# Drift-handling helper (B2 ↔ B3 bridge)
# ---------------------------------------------------------------------------


def _dispatch_drift_if_any(
    state: MutableMapping[str, Any],
    *,
    executor: Optional[StepExecutor] = None,
) -> list[DriftHandlingReceipt]:
    """If drift signals exist on ``state``, hand them to the A6 executor.

    A thin wrapper around :func:`remanifestation.handle_drift` (the A6
    canonical drift handler, landed on main via PR #177) so that all B2
    invocation points (after_agent, before_agent, before_tool, gate
    poll) share the same dispatch behaviour.

    Returns the handler receipts (empty list when no drift was queued).
    """
    if not pending_drift_signals(state):
        return []
    return handle_drift(state, executor=executor)


# ---------------------------------------------------------------------------
# Callback factories -- compose existing callbacks with B2 + B3
# ---------------------------------------------------------------------------


AgentCallback = Callable[[Any], Any]
ToolCallback = Callable[[Any, Mapping[str, Any], Any], Any]


def make_after_agent_with_consistency(
    original: Optional[AgentCallback],
    *,
    executor: Optional[StepExecutor] = None,
) -> AgentCallback:
    """Return an ``after_agent_callback`` composing ``original`` + A5 + B3.

    Order of operations:

    1. ``original(callback_context)`` -- run first so contracts, Timeline
       Guardian, approval gates, and output_key tagging remain
       authoritative.
    2. :func:`after_agent_consistency_check` -- A5 detects drift.
    3. :func:`remanifestation.handle_drift` -- A6 executor consumes
       any queued drift signals. Re-escalates to human L4 on
       exhaustion.

    Returns whatever ``original`` returned (typically ``None`` or a
    ``genai_types.Content`` skip marker).
    """

    def _chained(callback_context: Any) -> Any:
        result = None
        if original is not None:
            result = original(callback_context)

        after_agent_consistency_check(callback_context)
        _dispatch_drift_if_any(callback_context.state, executor=executor)
        return result

    _chained.__name__ = "after_agent_with_consistency"
    _chained.__qualname__ = "after_agent_with_consistency"
    _chained.__doc__ = (
        "ARCH-B2 after_agent_callback chain: "
        "original → A5 consistency check → B3 drift handler."
    )
    return _chained


def make_before_agent_with_consistency(
    original: Optional[AgentCallback],
    *,
    executor: Optional[StepExecutor] = None,
) -> AgentCallback:
    """Return a ``before_agent_callback`` composing ``original`` + A5 + B3.

    Stage-entry drift check catches the case described in #138: "a stage
    that starts with a stale ``ledger_revision_at_birth`` must trigger a
    consistency check and, if drift is found, re-escalate to the ledger
    or to human L4".

    Order of operations mirrors :func:`make_after_agent_with_consistency`:

    1. A5 consistency check -- reads the stage's derivation tag (if any)
       against the current ledger revision.
    2. B3 drift handler -- runs BEFORE the original callback so that a
       re-manifestation plan takes effect before the stage does work on
       a stale input.
    3. ``original(callback_context)`` -- last, so its behaviour
       (approval-gate waits, contract validation, etc.) is unchanged by
       the presence or absence of drift.

    If ``original`` returns a ``genai_types.Content`` skip marker, that
    value is returned verbatim (B2 does not override pipeline skips).
    """

    def _chained(callback_context: Any) -> Any:
        after_agent_consistency_check(callback_context)
        _dispatch_drift_if_any(callback_context.state, executor=executor)

        if original is not None:
            return original(callback_context)
        return None

    _chained.__name__ = "before_agent_with_consistency"
    _chained.__qualname__ = "before_agent_with_consistency"
    _chained.__doc__ = (
        "ARCH-B2 before_agent_callback chain: "
        "A5 consistency check → B3 drift handler → original."
    )
    return _chained


def make_before_tool_with_consistency(
    original: Optional[ToolCallback],
    *,
    executor: Optional[StepExecutor] = None,
) -> ToolCallback:
    """Return a ``before_tool_callback`` composing ``original`` + A5 + B3.

    Tool calls are the finest-grained drift-witness checkpoint (per #138
    "tool calls that mutate artifacts"). We run the A5 check FIRST so
    that a tool about to mutate a stale artifact can be blocked by the
    B3 re-manifestation path before the mutation happens.

    If the original tool callback returns a non-None value (rate-limit
    rejection, simulation override), we still surface that value -- the
    A5 check already ran, its drift signal (if any) is in the queue, and
    the ADK tool-call semantics say a non-None return from
    before_tool_callback short-circuits the tool.
    """

    def _chained(tool: Any, args: Mapping[str, Any], tool_context: Any) -> Any:
        before_tool_consistency_check(tool, args, tool_context)
        _dispatch_drift_if_any(tool_context.state, executor=executor)
        if original is not None:
            return original(tool, args, tool_context)
        return None

    _chained.__name__ = "before_tool_with_consistency"
    _chained.__qualname__ = "before_tool_with_consistency"
    _chained.__doc__ = (
        "ARCH-B2 before_tool_callback chain: "
        "A5 consistency check → B3 drift handler → original."
    )
    return _chained


# ---------------------------------------------------------------------------
# Gate-poll invocation point
# ---------------------------------------------------------------------------


def gate_poll_consistency_check(
    state: MutableMapping[str, Any],
    stage_name: str,
    *,
    executor: Optional[StepExecutor] = None,
) -> Optional[LedgerDrift]:
    """Run A5 + B3 from inside the approval-gate poll loop.

    Called by :mod:`callbacks.approval_gate` every poll interval. While
    the human is reviewing stage output, the ledger can still receive
    new L4 records (e.g. a concurrent reviewer tightening a constraint)
    -- this hook catches drift the moment it appears rather than waiting
    for the next stage-boundary check.

    Reconstruction is NOT gated recursively here (``gate=False``). The
    gate loop is already inside an approval gate; adding another
    would deadlock the poller on its own gate.

    Returns the drift signal if one was detected on this call, else
    ``None``. Any queued drift signals are drained and dispatched through
    B3 regardless of this call's own detection.
    """
    drift = check_consistency_at_gate(state, stage_name)
    _dispatch_drift_if_any(state, executor=executor)
    return drift


# ---------------------------------------------------------------------------
# Agent-tree wiring
# ---------------------------------------------------------------------------


def _iter_agent_tree(root: Any) -> Iterable[Any]:
    """Yield ``root`` and every sub-agent reachable via ``sub_agents``.

    Handles the SequentialAgent / LoopAgent / EvaluatorOptimizer
    compositions used by the documentary pipeline. Agents without a
    ``sub_agents`` attribute are treated as leaves.
    """
    seen: set[int] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        nid = id(node)
        if nid in seen:
            continue
        seen.add(nid)
        yield node
        subs = getattr(node, "sub_agents", None)
        if subs:
            for sub in subs:
                stack.append(sub)


def wire_consistency_checks_into_agents(
    root: Any,
    *,
    executor: Optional[StepExecutor] = None,
) -> list[str]:
    """Compose B2 callbacks onto every agent in ``root``'s tree.

    For each agent, preserves any existing ``after_agent_callback``,
    ``before_agent_callback``, and ``before_tool_callback`` by wrapping
    them with the corresponding ``make_*_with_consistency`` factory.
    Idempotent: agents tagged with :data:`_WIRED_ATTR` are skipped.

    Returns the list of agent names that were wired on this call
    (excluding those already wired). Useful for tests and startup logs.

    Args:
        root: A SequentialAgent / LoopAgent / Agent -- typically the
            master ``pipeline_agent``.
        executor: Optional step executor forwarded to A6's
            :func:`remanifestation.handle_drift`.
    """
    wired: list[str] = []
    for agent in _iter_agent_tree(root):
        if getattr(agent, _WIRED_ATTR, False):
            continue

        name = getattr(agent, "name", None) or type(agent).__name__

        orig_after = getattr(agent, "after_agent_callback", None)
        agent.after_agent_callback = make_after_agent_with_consistency(
            orig_after,
            executor=executor,
        )

        orig_before = getattr(agent, "before_agent_callback", None)
        agent.before_agent_callback = make_before_agent_with_consistency(
            orig_before,
            executor=executor,
        )

        # before_tool_callback is only meaningful on agents that own tools;
        # leaf Agent instances do. SequentialAgent / LoopAgent / composite
        # agents typically leave this attribute unset -- skip silently.
        if hasattr(agent, "before_tool_callback"):
            orig_tool = getattr(agent, "before_tool_callback", None)
            agent.before_tool_callback = make_before_tool_with_consistency(
                orig_tool,
                executor=executor,
            )

        try:
            setattr(agent, _WIRED_ATTR, True)
        except Exception:
            # Some ADK base classes use __slots__ and reject ad-hoc
            # attributes. That just means we cannot dedup, not that the
            # wiring failed -- warn once and continue.
            logger.debug(
                "consistency_gate: could not tag agent %r as wired "
                "(likely __slots__); idempotency disabled for this agent",
                name,
            )
        wired.append(name)

    logger.info(
        "consistency_gate: wired ARCH-B2 consistency checks into "
        "%d agent(s): %s",
        len(wired),
        wired,
    )
    return wired


__all__ = [
    "gate_poll_consistency_check",
    "make_after_agent_with_consistency",
    "make_before_agent_with_consistency",
    "make_before_tool_with_consistency",
    "wire_consistency_checks_into_agents",
]
