# 13 — escalation-supervisor (DeepAgent `SubAgent`)

The escalation supervisor is the **last agentic stop** before a human.
The main orchestrator (component 14) delegates to it whenever:

- The `production` SubAgent (component 10) returned a `request_escalation`
  payload after exhausting tactical recovery (component 12), or
- Iteration caps hit in the timing loop (component 05) or visual loop
  (component 09), or
- Any AGENTS.md invariant was violated (fail-closed path).

The escalation SubAgent's only job is to decide one of:
`fix` / `retry` / `skip` / `escalate_to_human` / `abort`, emit the
rationale, and return to the parent.

---

## Intent

1. Receive a structured diagnostic payload from the parent orchestrator.
2. Read the last N relevant artifacts (`read_file`) and the run's
   telemetry snapshot.
3. Reason over the failure mode and the AGENTS.md invariants.
4. Emit a decision object:
   ```json
   {
     "action": "fix|retry|skip|escalate_to_human|abort",
     "target": {"scope": "scene|stage|run", "id": "..."},
     "rationale": "...",
     "confidence": 0.0
   }
   ```
5. If `escalate_to_human`: also emit a structured summary for the
   approval gate (component 15) to show the operator.

This SubAgent does not execute recovery itself — the parent orchestrator
or the production SubAgent does, per the returned decision.

---

## Current implementation

Approximately `server/agents/production_supervisor.py` lines covering the
escalation ladder + `server/agents/recovery_agent.py` (rough ~400 LOC
combined). The escalation decision is entangled with the tactical
recovery code path.

---

## Target implementation

### SubAgent declaration

```python
# server/strands_agents/subagents/escalation.py
from deepagents.types import SubAgent
from strands.conversation.managers import SlidingWindowConversationManager

ESCALATION_SUPERVISOR_PROMPT = """\
You are the escalation supervisor. You decide what to do when the
production, timing, or visual stages have exhausted their tactical
recovery and cannot proceed.

You have five possible actions:
- fix      : prompt-level patch to a specific artifact (rare; usually
             the production subagent has already tried this).
- retry    : re-run a stage from scratch with cleared state (expensive).
- skip     : mark the scope as degraded, continue the pipeline.
- escalate_to_human : pause and request an operator decision.
- abort    : stop the run, mark it failed, preserve artifacts.

Process:
1. Read the diagnostic payload from the parent.
2. Read any referenced artifacts (failed frames, error logs, scene JSON).
3. Consult AGENTS.md invariants. Any violation → abort or
   escalate_to_human.
4. Emit a decision JSON matching the schema in component 13.
5. If escalate_to_human, also call request_human_approval with a
   one-paragraph summary the operator can act on.
"""

escalation_subagent: SubAgent = {
    "name": "escalation",
    "description": (
        "Escalation supervisor. Invoke when tactical recovery has "
        "failed. Returns a decision: fix / retry / skip / "
        "escalate_to_human / abort."
    ),
    "system_prompt": ESCALATION_SUPERVISOR_PROMPT,
    "tools": [
        read_file,                         # read scenes.json, production_report.json, etc.
        read_telemetry_snapshot,           # OTel-backed: recent errors + spans
        request_human_approval,            # component 15 interrupt tool
        write_file,                        # write escalation_decision.json
    ],
    "model": os.environ.get("STRANDS_THINKER_MODEL", "openai/gpt-4o"),
    "middleware": [
        # Keep the last 20 turns of diagnostic reasoning when the agent
        # needs to loop over multiple artifacts; we want full history
        # within a single decision, not compressed.
    ],
}
```

### Orchestrator invocation pattern

```python
# parent orchestrator, after production returned request_escalation
task(
    subagent_type="escalation",
    description=(
        "Production SubAgent exhausted tactical recovery for scene s3. "
        "Payload written to escalation_payload.json. Decide action."
    ),
)
# reads back escalation_decision.json after the task tool returns
```

### Decision contract

```python
# server/strands_agents/contracts/escalation.py
from typing import Literal, TypedDict

class EscalationTarget(TypedDict):
    scope: Literal["scene", "stage", "run"]
    id: str

class EscalationDecision(TypedDict):
    action: Literal["fix", "retry", "skip", "escalate_to_human", "abort"]
    target: EscalationTarget
    rationale: str
    confidence: float           # 0.0–1.0
    human_summary: str | None   # required when action == "escalate_to_human"
```

`ContractEnforcer` (component 01 hooks) validates the decision JSON
before the orchestrator consumes it.

### Why a SubAgent (not a plain tool)

- **Multi-turn reasoning with artifact reads.** Deciding `fix` vs
  `retry` vs `skip` typically requires reading the failed artifact,
  checking the error trace, and reading AGENTS.md. That's several tool
  calls with chained reasoning.
- **Isolated context.** The escalation SubAgent does not need the
  orchestrator's planning state, and vice versa.
- **Model choice.** This is the one SubAgent we intentionally point at
  the largest available reasoning model — the cost of a wrong decision
  here dwarfs the model cost.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    EscalationDecisionEvaluator(),        # LLM-as-judge, rubric:
                                           #   1.0 = correct
                                           #   0.5 = reasonable but suboptimal
                                           #   0.0 = harmful
    ToolSelectionAccuracyEvaluator(),     # did it read the right artifacts?
    ContractComplianceEvaluator(ESCALATION_CONTRACT),
    Equals("decision.action", expected),  # deterministic per-case
]
```

### Test cases (minimum 8)

| Case | Payload | Expected action |
|------|---------|-----------------|
| `transient_retry` | 1 worker-500, budget remaining | `retry` |
| `persistent_fail` | 3 retries + 1 fix failed, scene unfixable | `skip` (if style_lock permits) else `escalate_to_human` |
| `invariant_violation` | Audio artifact missing before dispatch | `abort` |
| `budget_exhausted_whole_stage` | 4 of 5 scenes still failing | `escalate_to_human` |
| `catastrophic_worker_crash` | All workers unhealthy | `escalate_to_human` |
| `style_drift` | Scene content doesn't match style_lock | `fix` (targeted concept regen) |
| `timing_loop_stuck` | 10 iterations, still off by 1s | `skip` (close enough given tolerance history) |
| `bad_payload` | Missing target scope | Contract violation → `abort` with rationale citing the contract |

### Simulators

- **Fixture payloads** under `tests/fixtures/escalation/*.json`. No
  external simulator; the escalation SubAgent reads files only.

### Thresholds

| Evaluator | Min score | Hard gate |
|-----------|-----------|-----------|
| `EscalationDecisionEvaluator` | 0.70 | No |
| `ToolSelectionAccuracyEvaluator` | 0.80 | Yes |
| `ContractComplianceEvaluator` | 1.00 | Yes |
| `Equals("decision.action", expected)` (where deterministic) | 1.00 | Yes |

---

## File layout

```
server/strands_agents/
├── subagents/
│   └── escalation.py
├── contracts/
│   └── escalation.py
└── evals/
    ├── evaluators/
    │   └── escalation_decision_evaluator.py
    └── experiments/
        └── escalation_experiment.json
```

---

## Acceptance criteria

- [ ] `escalation` SubAgent shipped as a `SubAgent` TypedDict.
- [ ] `EscalationDecision` TypedDict defined and enforced by contract.
- [ ] All 8 cases pass thresholds.
- [ ] `escalate_to_human` cases correctly call `request_human_approval`
      (component 15) with a non-empty `human_summary`.
- [ ] Decision JSON is always written to disk so the orchestrator can
      read it back after the `task` tool returns.
