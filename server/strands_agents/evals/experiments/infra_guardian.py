"""Infra-agent guardian experiment (slice 4a / 5a).

Drives :func:`strands_agents.infra_agent.guardian.should_destroy` over a
canonical suite of ``GuardianState`` × ``GuardianConfig`` × ``now``
triples and scores every run through deterministic
:class:`strands_evals.Evaluator` subclasses.

The guardian's decision core is pure, so this experiment runs offline —
no live VM, no Vast.ai API, no ``nvidia-smi`` subprocess. It nonetheless
exercises the full strands-evals surface (``Case``, ``Experiment``,
``Evaluator``, ``EvaluationOutput``) so the playground can drive it
from the UI exactly like the c01–c15 component experiments.

Cases cover:

* **idle_trigger** — bump older than ``idle_budget_s`` → ``"idle"``.
* **lifetime_trigger** — boot older than ``max_lifetime_budget_s``
  even with a fresh bump → ``"lifetime"``.
* **manual_trigger** — latched manual-destroy flag wins regardless of
  elapsed counters → ``"manual"``.
* **manual_precedence_over_budgets** — manual latched AND budgets blown
  → still ``"manual"`` (tier 1 precedence).
* **lifetime_precedence_over_idle** — both budgets blown, no manual →
  ``"lifetime"`` (tier 2 precedence).
* **bump_resets_idle** — bump just moved forward → no destruction.
* **within_budgets_no_destroy** — both counters under budget → ``None``.
* **exact_idle_boundary_triggers** — ``idle_elapsed == budget`` →
  ``"idle"`` (the boundary itself is the trigger; ``>=`` semantics).
* **exact_lifetime_boundary_triggers** — same, for the lifetime tier.
* **just_under_idle_boundary_no_destroy** — one second under budget
  stays alive. Complements the exact-boundary case.

Every case also records the deterministic ``idle_elapsed_s`` /
``lifetime_elapsed_s`` expectation so the telemetry-match evaluator can
pin them at the same time as the reason.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from strands_agents.infra_agent.guardian import (
    GuardianConfig,
    GuardianDecision,
    GuardianState,
    should_destroy,
)


#: Thresholds advertised to the playground catalog. Both evaluators
#: are hard gates — a guardian that picks the wrong reason or reports
#: the wrong elapsed counters is a safety regression.
INFRA_GUARDIAN_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "GuardianReasonEvaluator": (1.0, True),
    "GuardianElapsedEvaluator": (1.0, True),
}


# ── Cases ────────────────────────────────────────────────────────────

_IDLE_S: int = 60
_LIFETIME_S: int = 3600


def _case(
    name: str,
    *,
    boot_ts: float,
    last_bump_ts: float,
    manual: bool,
    now: float,
    idle_budget_s: int = _IDLE_S,
    lifetime_budget_s: int = _LIFETIME_S,
    expected_reason: str | None,
) -> Case[dict[str, Any], dict[str, Any]]:
    """Build one Case with pre-computed expected elapsed counters."""
    idle_elapsed = max(0.0, now - last_bump_ts)
    lifetime_elapsed = max(0.0, now - boot_ts)
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"infra-guardian-{name}",
        input={
            "boot_ts": boot_ts,
            "last_bump_ts": last_bump_ts,
            "manual_destroy_requested": manual,
            "now": now,
            "idle_budget_s": idle_budget_s,
            "max_lifetime_budget_s": lifetime_budget_s,
        },
        expected_output={
            "reason": expected_reason,
            "idle_elapsed_s": idle_elapsed,
            "lifetime_elapsed_s": lifetime_elapsed,
        },
        metadata={
            "expected_reason": expected_reason,
            "expected_idle_elapsed_s": idle_elapsed,
            "expected_lifetime_elapsed_s": lifetime_elapsed,
        },
    )


def infra_guardian_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the canonical suite for the guardian decision core."""
    return [
        _case(
            "idle_trigger",
            boot_ts=0.0,
            last_bump_ts=0.0,
            manual=False,
            now=float(_IDLE_S + 1),
            expected_reason="idle",
        ),
        _case(
            "lifetime_trigger",
            boot_ts=0.0,
            last_bump_ts=float(_LIFETIME_S + 1),
            manual=False,
            now=float(_LIFETIME_S + 1),
            expected_reason="lifetime",
        ),
        _case(
            "manual_trigger",
            boot_ts=0.0,
            last_bump_ts=0.0,
            manual=True,
            now=1.0,
            expected_reason="manual",
        ),
        _case(
            "manual_precedence_over_budgets",
            boot_ts=0.0,
            last_bump_ts=0.0,
            manual=True,
            now=float(_LIFETIME_S + 999),
            expected_reason="manual",
        ),
        _case(
            "lifetime_precedence_over_idle",
            boot_ts=0.0,
            last_bump_ts=0.0,
            manual=False,
            now=float(_LIFETIME_S + 1),
            expected_reason="lifetime",
        ),
        _case(
            "bump_resets_idle",
            boot_ts=0.0,
            last_bump_ts=100.0,
            manual=False,
            now=100.0 + float(_IDLE_S - 1),
            expected_reason=None,
        ),
        _case(
            "within_budgets_no_destroy",
            boot_ts=0.0,
            last_bump_ts=0.0,
            manual=False,
            now=float(_IDLE_S - 1),
            expected_reason=None,
        ),
        _case(
            "exact_idle_boundary_triggers",
            boot_ts=0.0,
            last_bump_ts=0.0,
            manual=False,
            now=float(_IDLE_S),
            expected_reason="idle",
        ),
        _case(
            "exact_lifetime_boundary_triggers",
            boot_ts=0.0,
            last_bump_ts=float(_LIFETIME_S),
            manual=False,
            now=float(_LIFETIME_S),
            expected_reason="lifetime",
        ),
        _case(
            "just_under_idle_boundary_no_destroy",
            boot_ts=0.0,
            last_bump_ts=0.0,
            manual=False,
            now=float(_IDLE_S - 1),
            expected_reason=None,
        ),
    ]


# ── Task adapter ─────────────────────────────────────────────────────


def infra_guardian_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Run ``should_destroy`` against the case's state/config/now triple.

    Returns the evaluate-friendly envelope:

    * ``output.reason`` — ``"idle" | "lifetime" | "manual" | None``.
    * ``output.idle_elapsed_s`` / ``output.lifetime_elapsed_s`` — the
      computed counters (telemetry-match evaluator keys off these).
    * ``trajectory`` — ``["should_destroy"]`` always. The guardian is
      one call; the trajectory is flat on purpose.
    """
    payload = case.input or {}
    config = GuardianConfig(
        idle_budget_s=int(payload["idle_budget_s"]),
        max_lifetime_budget_s=int(payload["max_lifetime_budget_s"]),
    )
    state = GuardianState(
        boot_ts=float(payload["boot_ts"]),
        last_bump_ts=float(payload["last_bump_ts"]),
        manual_destroy_requested=bool(payload["manual_destroy_requested"]),
    )
    decision: GuardianDecision = should_destroy(
        state=state,
        config=config,
        now=float(payload["now"]),
    )
    return {
        "output": {
            "reason": decision.reason,
            "idle_elapsed_s": decision.idle_elapsed_s,
            "lifetime_elapsed_s": decision.lifetime_elapsed_s,
            "should_destroy": decision.should_destroy,
        },
        "trajectory": ["should_destroy"],
        "metadata": {
            "idle_budget_s": config.idle_budget_s,
            "max_lifetime_budget_s": config.max_lifetime_budget_s,
        },
    }


# ── Evaluators ───────────────────────────────────────────────────────


class GuardianReasonEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Compare ``actual.reason`` to ``metadata['expected_reason']``.

    Strands-evals ships ``Equals`` but it compares the whole
    ``actual_output`` dict against the whole ``expected_output`` dict,
    which would conflate the reason-match check with the
    elapsed-counter check. We want them to score independently so a
    reason regression is distinguishable from a float-rounding drift.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        expected = metadata.get("expected_reason")
        got = actual.get("reason")
        match = got == expected
        return [
            EvaluationOutput(
                score=1.0 if match else 0.0,
                test_pass=match,
                reason=(
                    f"decision.reason={got!r} "
                    f"{'matches' if match else 'does not match'} "
                    f"expected={expected!r}"
                ),
                label="reason_match" if match else "reason_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class GuardianElapsedEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin ``idle_elapsed_s`` and ``lifetime_elapsed_s`` to expected.

    The guardian reports both counters even when it returns no reason,
    because the agent surfaces them via ``/infra/status`` for operators
    and for the lessons ledger. Drift here would corrupt the ledger
    even if the destruction decision is still right.

    Tolerance is ``1e-6`` — the values are constructed by subtraction
    in the task, so single-ULP drift is possible on some platforms.
    """

    TOLERANCE: float = 1e-6

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        expected_idle = float(metadata.get("expected_idle_elapsed_s", 0.0))
        expected_lifetime = float(metadata.get("expected_lifetime_elapsed_s", 0.0))
        actual_idle = float(actual.get("idle_elapsed_s", -1.0))
        actual_lifetime = float(actual.get("lifetime_elapsed_s", -1.0))

        idle_ok = abs(actual_idle - expected_idle) < self.TOLERANCE
        lifetime_ok = abs(actual_lifetime - expected_lifetime) < self.TOLERANCE
        both_ok = idle_ok and lifetime_ok
        return [
            EvaluationOutput(
                score=1.0 if both_ok else 0.0,
                test_pass=both_ok,
                reason=(
                    f"idle_elapsed_s={actual_idle} "
                    f"(expected {expected_idle}, {'ok' if idle_ok else 'drift'}); "
                    f"lifetime_elapsed_s={actual_lifetime} "
                    f"(expected {expected_lifetime}, "
                    f"{'ok' if lifetime_ok else 'drift'})"
                ),
                label="elapsed_match" if both_ok else "elapsed_drift",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


# ── Experiment factory ───────────────────────────────────────────────


def build_infra_guardian_experiment() -> Experiment[dict[str, Any], dict[str, Any]]:
    """Assemble the guardian :class:`Experiment`.

    Returns:
        A fully wired experiment covering the nine canonical Cases and
        the two deterministic evaluators. Ready for
        :meth:`Experiment.run_evaluations` or to be surfaced through
        the playground's ``/playground/components/{id}/evaluate``
        endpoint.
    """
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=infra_guardian_cases(),
        evaluators=[
            GuardianReasonEvaluator(),
            GuardianElapsedEvaluator(),
        ],
    )


__all__ = [
    "INFRA_GUARDIAN_EVALUATOR_THRESHOLDS",
    "GuardianElapsedEvaluator",
    "GuardianReasonEvaluator",
    "build_infra_guardian_experiment",
    "infra_guardian_cases",
    "infra_guardian_task",
]
