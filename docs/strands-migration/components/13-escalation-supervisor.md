# 13 — escalation-supervisor

Top-level recovery agent. Handles persistent failures surfaced by the
production supervisor (10) or the recovery agents (12). Consults the
user via an `Interrupt` if necessary. Terminates every run with one of
`fix | retry | skip | escalate | abort`.

---

## Intent

Given an `escalation_request`, produce a decision that the pipeline
graph (14) acts on. Runs multi-turn: may ask the user for clarification
via `Interrupt`, may request diagnostic artifacts (logs, recent jobs),
may direct a targeted remanifest.

---

## Current implementation

[`server/agents/escalation_supervisor.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/agents/escalation_supervisor.py).
Currently coordinates with `preview_critic.py` and the diagnostic
classifier via shared state.

---

## Strands implementation

Single file: `server/strands_agents/escalation_supervisor.py`.

### System prompt

~1 000 tokens. Covers:

- Role ("you are the Escalation Supervisor, the final recovery step").
- Decision vocabulary (`fix`, `retry`, `skip`, `escalate`, `abort`) and
  when each is appropriate.
- How to use `request_user_input` (component 15 integration).
- Hard constraint: every run must terminate with exactly one decision;
  decisions are logged to `recovery_log`.

### Tools (5)

```python
@tool
def fetch_diagnostics(artifact_id: str) -> dict:
    """Pull recent logs, worker status, job history for artifact_id."""

@tool
def request_remanifest(artifact_id: str, hint: str) -> dict:
    """Delegate to remanifestation agent (12); returns revised concept."""

@tool
def consult_user(question: str, context: dict) -> dict:
    """Pause the run via Interrupt; resumes with the user's reply."""

@tool
def emit_decision(
    artifact_id: str,
    action: Literal["fix", "retry", "skip", "escalate", "abort"],
    reason: str,
) -> dict: ...

@tool(context=True)
async def persist_escalation(context, decision: dict) -> dict: ...
```

### Hooks

```python
hooks = [
    ContractEnforcer(ESCALATION_CONTRACT),
    RecoveryLogger(),
    InterruptBridge(interrupt_id_prefix="escalation"),
]
```

`InterruptBridge` translates `consult_user` tool calls into `Interrupt`
exceptions (see component 15).

### Conversation manager

`SlidingWindowConversationManager(window_size=60)`. Multi-turn user
interaction + diagnostic gathering; we want the history.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(ESCALATION_CONTRACT),
    EscalationDecisionEvaluator(),
    InteractionsEvaluator(),     # multi-turn quality when user is consulted
    GoalSuccessRateEvaluator(),
    CritiqueStoreEvaluator(artifact_type="escalation"),
]
```

### Test cases (minimum 8 — uses `ESCALATION_ACTOR_SIMULATOR`)

| Case name | Seed failure | User behaviour (simulator) | Expected decision |
|-----------|--------------|---------------------------|-------------------|
| `transient_error_retry` | CUDA OOM 3x seen | ignores (autonomous) | retry |
| `persistent_error_escalate` | wrong-style 3x | answers one clarifying Q | fix (remanifest) |
| `fixable_error_with_hint` | prompt-too-vague | gives a hint | fix (remanifest) |
| `catastrophic_error_abort` | all workers 500 | no user needed | abort |
| `confusing_mixed_signal` | contradictory errors | asks user; user answers | fix OR skip |
| `user_overrides` | any error | user says "skip this clip" | skip |
| `user_requests_diagnostic` | any error | user asks to see logs; then decides | fix or abort |
| `unresponsive_user` | any error | simulator never replies | escalate (flags run stuck) |

### Simulators

`ESCALATION_ACTOR_SIMULATOR` (mandatory),
`GPU_WORKER_SIMULATOR` (to produce the seed failures).

### Thresholds

- `ContractComplianceEvaluator` = 1.00 (hard)
- `EscalationDecisionEvaluator` ≥ 0.70 (soft)
- `InteractionsEvaluator` ≥ 0.60 (soft)
- `GoalSuccessRateEvaluator` ≥ 0.80 (hard)

---

## File layout

```
server/strands_agents/
├── escalation_supervisor.py                      # ~350 LOC
└── hooks/
    └── interrupt_bridge.py                       # ~80 LOC
```

---

## Acceptance criteria

- [ ] Every test case terminates with exactly one decision in `recovery_log`.
- [ ] `unresponsive_user` triggers a bounded wait then escalates — no infinite hang.
- [ ] `Interrupt` resume path tested (suspend → resume → decision).
- [ ] Experiment passes thresholds.
