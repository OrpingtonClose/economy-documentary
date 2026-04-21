"""Escalation supervisor — the last agentic stop before a human.

Component 13. Receives a structured diagnostic payload from the parent
orchestrator (after tactical recovery in component 12 exhausted, or an
iteration cap hit, or an AGENTS.md invariant was violated) and emits
one decision:

* ``fix``                — apply a targeted state patch and re-run.
* ``retry``              — re-run unchanged (transient failure path).
* ``skip``               — accept the failure, continue degraded.
* ``escalate_to_human``  — pause and hand off to a human operator.
* ``abort``              — stop the run; preserve artifacts.

The decision is emitted as an ``EscalationDecision`` TypedDict and
additionally written to disk so the orchestrator can read it back after
the DeepAgent ``task`` tool returns.

Two deterministic tools underpin the SubAgent so the full 8-case eval
suite runs in CI without model credentials:

* :func:`decide_escalation_action` — heuristic rule table over the
  diagnostic payload. Same input always yields the same output.
* :func:`write_escalation_decision` — serialises the decision dict to
  JSON on disk (used by the orchestrator read-back flow).

The remaining tools (:func:`read_file`, :func:`read_telemetry_snapshot`,
:func:`request_human_approval`) are thin stubs the SubAgent calls during
real runs; they are wired here so the ``SubAgent`` TypedDict stays
complete, but the eval harness exercises :func:`decide_escalation_action`
directly for determinism.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal, TypedDict

from deepagents import SubAgent
from strands import tool

logger = logging.getLogger(__name__)

Action = Literal["fix", "retry", "skip", "escalate_to_human", "abort"]
Scope = Literal["scene", "stage", "run"]

_VALID_ACTIONS: frozenset[str] = frozenset(
    {"fix", "retry", "skip", "escalate_to_human", "abort"}
)
_VALID_SCOPES: frozenset[str] = frozenset({"scene", "stage", "run"})


class EscalationTarget(TypedDict):
    """The scope + id this decision applies to."""

    scope: Scope
    id: str


class EscalationDecision(TypedDict, total=False):
    """Structured output from the escalation supervisor.

    Fields:
        action: The recovery action chosen.
        target: Which scope + id the decision applies to.
        rationale: Short justification (one or two sentences).
        confidence: ``0.0`` – ``1.0``. Low confidence on bad payloads.
        human_summary: Non-empty string when ``action`` is
            ``"escalate_to_human"``; ``None`` otherwise.
        state_patches: The concrete patch to apply when
            ``action`` is ``"fix"``; ``None`` otherwise.
    """

    action: Action
    target: EscalationTarget
    rationale: str
    confidence: float
    human_summary: str | None
    state_patches: dict[str, Any] | None


# ── Decision rule table ───────────────────────────────────────────────

def _normalise_target(payload: dict[str, Any]) -> tuple[EscalationTarget, bool]:
    """Extract + validate the target block.

    Returns:
        ``(target, ok)`` where ``ok`` is ``False`` when the payload
        violates the contract (missing scope / missing id / bad scope).
        The caller uses that to short-circuit to an ``abort`` with a
        ``contract_violation`` rationale.
    """

    target = payload.get("target") or {}
    scope_raw = str(target.get("scope", "")).strip().lower()
    id_raw = str(target.get("id", "")).strip()
    if scope_raw not in _VALID_SCOPES or not id_raw:
        return (
            EscalationTarget(scope="run", id=id_raw or "unknown"),
            False,
        )
    return EscalationTarget(scope=scope_raw, id=id_raw), True  # type: ignore[typeddict-item]


def _classify(payload: dict[str, Any]) -> EscalationDecision:
    """Deterministic rule table over a diagnostic payload."""

    target, target_ok = _normalise_target(payload)
    error_class = str(payload.get("error_class", "")).strip().lower()
    error_message = str(payload.get("error_message", "")).strip() or "unknown"

    if not target_ok:
        return EscalationDecision(
            action="abort",
            target=target,
            rationale=(
                "contract_violation: diagnostic payload is missing a valid "
                "target.scope / target.id — ESCALATION_CONTRACT requires "
                "a fully-populated target block"
            ),
            confidence=0.95,
            human_summary=None,
            state_patches=None,
        )

    if (
        payload.get("worker_crashed")
        or error_class == "catastrophic_worker_crash"
    ):
        healthy = payload.get("workers_healthy")
        has_any_healthy = bool(healthy) if healthy is not None else False
        return EscalationDecision(
            action="escalate_to_human",
            target=target,
            rationale=(
                "catastrophic worker crash — no healthy worker pool "
                "available to retry against"
            ),
            confidence=0.9,
            human_summary=(
                f"Catastrophic worker crash on {target['scope']}="
                f"{target['id']}: {error_message}. "
                f"{'Partial' if has_any_healthy else 'All'} workers "
                "unhealthy. Operator needs to restore worker pool "
                "before resume."
            ),
            state_patches=None,
        )

    if error_class == "invariant_violation":
        return EscalationDecision(
            action="abort",
            target=target,
            rationale=(
                "AGENTS.md invariant violated — fail-closed per "
                "pipeline policy"
            ),
            confidence=0.95,
            human_summary=None,
            state_patches=None,
        )

    if payload.get("budget_over") or error_class == "budget_exhausted_whole_stage":
        return EscalationDecision(
            action="escalate_to_human",
            target=target,
            rationale=(
                "stage-wide budget exhausted — cannot continue without "
                "operator approval for additional spend"
            ),
            confidence=0.85,
            human_summary=(
                f"Budget exhausted on {target['scope']}={target['id']}: "
                f"{payload.get('failed_scenes', 'several')} of "
                f"{payload.get('total_scenes', 'total')} scenes still "
                "failing. Operator must decide: continue degraded, "
                "raise budget, or abort."
            ),
            state_patches=None,
        )

    if error_class == "style_drift":
        patch = payload.get("suggested_patch") or {
            "regenerate_concept": True,
            "enforce_style_lock": True,
        }
        return EscalationDecision(
            action="fix",
            target=target,
            rationale=(
                "style drift against locked style_family — targeted "
                "concept regeneration is cheaper than a full retry"
            ),
            confidence=0.8,
            human_summary=None,
            state_patches=dict(patch),
        )

    retries = int(payload.get("retries", 0))
    retries_max = int(payload.get("retries_max", 3))

    if error_class == "transient_retry" or error_class == "transient":
        if retries < retries_max:
            return EscalationDecision(
                action="retry",
                target=target,
                rationale=(
                    f"transient failure — retry {retries + 1}/"
                    f"{retries_max} with budget remaining"
                ),
                confidence=0.8,
                human_summary=None,
                state_patches=None,
            )
        return EscalationDecision(
            action="escalate_to_human",
            target=target,
            rationale=(
                "transient retries exhausted — pattern is no longer "
                "transient and needs human review"
            ),
            confidence=0.75,
            human_summary=(
                f"{target['scope']}={target['id']} failed transiently "
                f"{retries_max} times. Retry budget exhausted; operator "
                "review required."
            ),
            state_patches=None,
        )

    if error_class == "persistent_fail" or error_class == "persistent":
        style_lock_permits_skip = bool(payload.get("style_lock_permits_skip", True))
        if style_lock_permits_skip:
            return EscalationDecision(
                action="skip",
                target=target,
                rationale=(
                    "persistent failure on a non-critical artifact and "
                    "style_lock permits degradation — continue pipeline"
                ),
                confidence=0.7,
                human_summary=None,
                state_patches=None,
            )
        return EscalationDecision(
            action="escalate_to_human",
            target=target,
            rationale=(
                "persistent failure but style_lock forbids skipping — "
                "operator must decide"
            ),
            confidence=0.8,
            human_summary=(
                f"Persistent failure on {target['scope']}="
                f"{target['id']}: {error_message}. style_lock forbids "
                "skipping; operator review required."
            ),
            state_patches=None,
        )

    if error_class == "timing_loop_stuck" or error_class == "timing_stuck":
        return EscalationDecision(
            action="skip",
            target=target,
            rationale=(
                "timing loop exhausted iterations with residual deviation "
                "within operator-acceptable tolerance history — accept "
                "degraded timing"
            ),
            confidence=0.65,
            human_summary=None,
            state_patches=None,
        )

    return EscalationDecision(
        action="escalate_to_human",
        target=target,
        rationale=(
            f"unknown error_class {error_class!r} — safe default is "
            "operator review"
        ),
        confidence=0.4,
        human_summary=(
            f"Unknown failure on {target['scope']}={target['id']}: "
            f"{error_message}. No rule matched — operator review "
            "required."
        ),
        state_patches=None,
    )


def _enforce_human_summary(decision: EscalationDecision) -> EscalationDecision:
    """Fail-closed: every ``escalate_to_human`` needs a human summary."""

    if decision["action"] == "escalate_to_human" and not decision.get("human_summary"):
        patched = copy.deepcopy(decision)
        patched["human_summary"] = (
            f"Escalation on {decision['target']['scope']}="
            f"{decision['target']['id']}: {decision['rationale']}"
        )
        return patched
    return decision


# ── Tools wired into the SubAgent ────────────────────────────────────

@tool
def decide_escalation_action(diagnostic_payload: dict[str, Any]) -> dict[str, Any]:
    """Classify a diagnostic payload into an escalation decision.

    Deterministic: same input always yields the same decision.

    Args:
        diagnostic_payload: The structured payload produced by the
            parent orchestrator after tactical recovery gave up. See
            ``docs/strands-migration/components/13-escalation-supervisor.md``
            for the schema.

    Returns:
        An :class:`EscalationDecision` dict.
    """

    decision = _classify(diagnostic_payload)
    return dict(_enforce_human_summary(decision))


@tool
def write_escalation_decision(
    decision: dict[str, Any], output_path: str
) -> str:
    """Serialise an escalation decision to JSON on disk.

    The orchestrator reads this file back after the DeepAgent
    ``task`` tool returns, so the path must be stable across the
    parent and child contexts.

    Args:
        decision: The :class:`EscalationDecision` dict.
        output_path: Absolute file path the decision is written to.

    Returns:
        The absolute path the decision was written to.
    """

    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(decision, handle, indent=2, sort_keys=True, default=str)
    logger.debug(
        "path=<%s>, action=<%s> | wrote escalation decision",
        path,
        decision.get("action"),
    )
    return str(path)


@tool
def read_file(path: str) -> str:
    """Read a referenced artifact (scenes.json, error logs, etc.).

    Thin wrapper used by the SubAgent during live runs; the eval
    harness exercises the decision logic via
    :func:`decide_escalation_action` directly.
    """

    resolved = Path(path).expanduser().resolve()
    with resolved.open("r", encoding="utf-8") as handle:
        return handle.read()


@tool
def read_telemetry_snapshot(target_id: str) -> dict[str, Any]:
    """Return a minimal telemetry snapshot for the target.

    Placeholder: returns an empty snapshot until the Langfuse-backed
    implementation lands in component 14. The SubAgent tolerates
    empty snapshots so the interface is stable.
    """

    logger.debug("target_id=<%s> | telemetry snapshot stub invoked", target_id)
    return {"target_id": target_id, "recent_errors": [], "recent_spans": []}


@tool
def request_human_approval(summary: str) -> dict[str, Any]:
    """Pause and hand the decision off to a human operator.

    Wired into component 15 (approval gates) as a LangChain
    ``interrupt_on`` tool. Until then this is a stub that records the
    summary and returns ``{"status": "pending"}`` so the SubAgent's
    decision flow remains observable in CI.
    """

    logger.info("summary=<%s> | request_human_approval (stub)", summary)
    return {"status": "pending", "summary": summary}


# ── SubAgent declaration ─────────────────────────────────────────────

ESCALATION_SUPERVISOR_PROMPT = """\
You are the escalation supervisor. You decide what to do when the
production, timing, or visual stages have exhausted their tactical
recovery and cannot proceed.

You have five possible actions:
- fix                : apply a targeted state_patches dict and re-run
                       the offending scope (rare; production usually
                       tried this first).
- retry              : re-run the scope unchanged — only for transient
                       failures with retry budget remaining.
- skip               : mark the scope as degraded and continue the
                       pipeline — only when style_lock permits.
- escalate_to_human  : pause and ask an operator; always populate
                       ``human_summary`` with one actionable paragraph.
- abort              : stop the run, preserve artifacts — reserved for
                       AGENTS.md invariant violations and contract
                       violations.

Process:
1. Read the diagnostic payload from the parent.
2. Read any referenced artifacts (failed frames, error logs, scene
   JSON) with ``read_file``.
3. Check ``read_telemetry_snapshot`` for the target id.
4. Consult AGENTS.md invariants. Any violation → abort or
   escalate_to_human.
5. Call ``decide_escalation_action`` with the full payload. Treat the
   returned dict as authoritative unless new evidence from step 2/3
   contradicts it — in which case explain why in ``rationale``.
6. Always call ``write_escalation_decision`` so the orchestrator can
   read the decision back after the ``task`` tool returns.
7. If the decision is ``escalate_to_human``, also call
   ``request_human_approval`` with the ``human_summary`` field.
"""


def build_escalation_supervisor(
    *,
    model: str | None = None,
) -> SubAgent:
    """Construct the escalation supervisor :class:`SubAgent`.

    Args:
        model: Optional override. Defaults to the environment variable
            ``STRANDS_THINKER_MODEL`` so this SubAgent points at the
            largest available reasoning model in production.

    Returns:
        A fully-populated :class:`SubAgent` TypedDict ready to be
        registered with ``create_deep_agent(..., subagents=[...])``.
    """

    resolved_model = model or os.environ.get(
        "STRANDS_THINKER_MODEL", "openai/gpt-4o"
    )
    return SubAgent(
        name="escalation",
        description=(
            "Escalation supervisor. Invoke when tactical recovery has "
            "failed. Returns a decision: fix / retry / skip / "
            "escalate_to_human / abort, and writes it to disk."
        ),
        system_prompt=ESCALATION_SUPERVISOR_PROMPT,
        tools=[
            decide_escalation_action,
            write_escalation_decision,
            read_file,
            read_telemetry_snapshot,
            request_human_approval,
        ],
        model=resolved_model,
    )


__all__ = [
    "Action",
    "ESCALATION_SUPERVISOR_PROMPT",
    "EscalationDecision",
    "EscalationTarget",
    "Scope",
    "build_escalation_supervisor",
    "decide_escalation_action",
    "read_file",
    "read_telemetry_snapshot",
    "request_human_approval",
    "write_escalation_decision",
]
