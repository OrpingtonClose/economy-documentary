"""Escalation actor cases per ``SIMULATION.md`` §3.

Defines the eight-case catalogue that drives component 13
(escalation-supervisor) and any orchestration experiment that needs a
simulated human operator. Each case is a :class:`~strands_evals.case.Case`
with metadata linking it to the expected supervisor outcome — that
metadata is consumed by :class:`EscalationDecisionEvaluator` and
:class:`InteractionsEvaluator`.
"""

from __future__ import annotations

from strands_evals.case import Case  # type: ignore[import-not-found]
from strands_evals.simulation.actor_simulator import ActorSimulator  # type: ignore[import-not-found]

MAX_TURNS = 8
"""Upper bound on turns per simulated escalation.

Empirically, production escalations resolve in ≤ 6 turns in 95% of
cases; 8 gives headroom without letting the conversation sprawl.
"""

ESCALATION_CASES: list[Case] = [
    Case(
        name="transient_error_retry",
        input="Scene 4 TTS returned 'CUDA OOM' once. What should we do?",
        expected_output="retry",
        metadata={
            "scenario": "Worker returned 'CUDA OOM' once",
            "expected_outcome": "retry",
            "notes": "Supervisor should instruct a retry; no escalation yet.",
        },
    ),
    Case(
        name="persistent_error_escalate",
        input="Scene 2 LTX has failed 3 times in a row with 'model checkpoint missing'.",
        expected_output="escalate",
        metadata={
            "scenario": "Same worker fails 3x in a row with checkpoint error",
            "expected_outcome": "escalate",
            "notes": "Supervisor escalates to human, halts pipeline.",
        },
    ),
    Case(
        name="fixable_error_with_hint",
        input="Scene 4 runs 1.8 s over its target duration. The scenario_refiner can re-time.",
        expected_output="fix",
        metadata={
            "scenario": "Scene 4 exceeded target duration",
            "expected_outcome": "fix",
            "notes": "Supervisor invokes scenario_refiner, not abort.",
        },
    ),
    Case(
        name="catastrophic_error_abort",
        input="Both GPU workers are dead. No spares available.",
        expected_output="abort",
        metadata={
            "scenario": "Two GPU workers dead, no spares",
            "expected_outcome": "abort",
            "notes": "Supervisor aborts, persists state for resume.",
        },
    ),
    Case(
        name="confusing_mixed_signal",
        input="Worker A is degraded (queue depth 5), worker B is ok. Incoming job.",
        expected_output="partition",
        metadata={
            "scenario": "One worker degraded, another ok",
            "expected_outcome": "partition",
            "notes": "Supervisor partitions workload onto healthy worker.",
        },
    ),
    Case(
        name="user_overrides_suggestion",
        input="I reject the refiner's proposed fix. Use my override instead.",
        expected_output="accept_override",
        metadata={
            "scenario": "User rejects proposed fix",
            "expected_outcome": "accept_override",
            "notes": "Supervisor accepts override, logs rationale.",
        },
    ),
    Case(
        name="user_requests_diagnostic",
        input="Show me the full error trace for the scene 2 failure.",
        expected_output="diagnostic",
        metadata={
            "scenario": "User asks for structured error diagnostic",
            "expected_outcome": "diagnostic",
            "notes": "Supervisor returns structured diagnostic.",
        },
    ),
    Case(
        name="unresponsive_user",
        input="",
        expected_output="persist_and_interrupt",
        metadata={
            "scenario": "User has not replied for 2 turns",
            "expected_outcome": "persist_and_interrupt",
            "notes": "Supervisor persists state and emits interrupt.",
        },
    ),
]


def build_escalation_actor(case: Case, *, model: str | None = None) -> ActorSimulator:
    """Build an :class:`ActorSimulator` for ``case``.

    Thin convenience wrapper over
    :meth:`ActorSimulator.from_case_for_user_simulator` that pins
    ``max_turns`` to :data:`MAX_TURNS` so every escalation experiment
    uses the same ceiling without having to remember the number.

    Args:
        case: One of :data:`ESCALATION_CASES` (or a custom case with
            the same metadata shape).
        model: Optional model identifier for the actor LLM.

    Returns:
        Configured :class:`ActorSimulator` ready for
        :meth:`ActorSimulator.act` turn-taking.
    """
    return ActorSimulator.from_case_for_user_simulator(
        case,
        model=model,
        max_turns=MAX_TURNS,
    )
