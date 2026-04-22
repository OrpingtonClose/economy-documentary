# 12 — recovery-agents

Two LLM agents — the **diagnostic classifier** and the **remanifestation
agent** — that live together in one module because they share recovery
vocabulary and are only ever invoked by the production supervisor (10)
or the escalation supervisor (13).

---

## Intent

### 12a — diagnostic classifier

Given a failure event (`job_id`, error string, `concept`, recent tool
history) classify it as:

- `transient` — infrastructure hiccup; retry.
- `fixable` — wrong prompt / params; remanifest.
- `persistent` — same error 3x; escalate to 13.
- `catastrophic` — infrastructure broken; abort.

### 12b — remanifestation agent

Given a `fixable` classification + a concept, emit a revised concept
that addresses the failure cause. Preserves `phrase_id`, `scene_id`; may
change prompt, negative_prompt, params, shot_type, camera_movement.

---

## Current implementation

- [`server/agents/diagnostic_classifier.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/agents/diagnostic_classifier.py)
- [`server/agents/remanifestation_agent.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/agents/remanifestation_agent.py)
- [`server/callbacks/remanifestation.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/callbacks/remanifestation.py)

---

## Strands implementation

Single file: `server/strands_agents/recovery_agents.py` containing two
factory functions: `build_diagnostic_classifier()` and
`build_remanifestation_agent()`.

### Diagnostic classifier

#### System prompt

~500 tokens. Classification vocabulary, decision tree:

- `"CUDA OOM"`, `"connection reset"`, `"model reload"` → transient.
- Output doesn't match `style_lock` → fixable (suggest prompt tweak).
- Same error 3+ times → persistent.
- GPU worker returns 500 / no workers available → catastrophic.

#### Tools (2)

```python
@tool
def classify(error: str, recent_history: list[dict], concept: dict) -> dict:
    """Returns {"class": transient|fixable|persistent|catastrophic, "hint": str}"""

@tool(context=True)
async def persist_classification(context, artifact_id: str, classification: dict) -> dict: ...
```

#### Hooks

```python
hooks = [
    ContractEnforcer(RECOVERY_CONTRACT_CLASSIFIER),    # see CONTRACTS.md §6
    RecoveryLogger(),                                  # appends to recovery_log
]
```

#### Conversation manager

`SlidingWindowConversationManager(window_size=40)`. Recovery may bounce
between classifier and remanifester; we want enough history to detect
"same error 3x".

---

### Remanifestation agent

#### System prompt

~400 tokens. Fixable-error vocabulary → prompt-tweak vocabulary. Preserve
`phrase_id`, `scene_id`, `duration_sec`, `style_lock_applied` flag.

#### Tools (2)

```python
@tool
def propose_revised_concept(
    original_concept: dict, error: str, hint: str, style_lock: dict,
) -> dict:
    """Returns a revised visual_concepts[] entry."""

@tool
def diff_concept(original: dict, revised: dict) -> dict:
    """Returns {"changed_fields": [...]} for logging + evaluation."""
```

#### Hooks

```python
hooks = [
    ContractEnforcer(RECOVERY_CONTRACT_REMANIFESTER),
    StyleLockEnforcer(),
    RecoveryLogger(),
]
```

#### Conversation manager

`SlidingWindowConversationManager(window_size=20)`.

---

## Evals harness

### Evaluator stack (both agents share this experiment)

```python
evaluators = [
    ContractComplianceEvaluator(RECOVERY_CONTRACT_CLASSIFIER),
    ContractComplianceEvaluator(RECOVERY_CONTRACT_REMANIFESTER),
    EscalationDecisionEvaluator(),
    TrajectoryEvaluator(),
    OutputEvaluator(rubric=_REMANIFEST_RUBRIC),
]
```

`_REMANIFEST_RUBRIC`: "revised concept preserves phrase_id, scene_id,
duration_sec; at least one actionable change; still passes style_lock".

### Test cases (minimum 8)

| Case name | Error | Expected classification | Expected remanifest? |
|-----------|-------|-------------------------|----------------------|
| `cuda_oom` | "CUDA OOM" | transient | no |
| `connection_reset` | "connection reset by peer" | transient | no |
| `wrong_style_output` | "output style doesn't match cinematic_documentary" | fixable | yes (prompt tweak) |
| `prompt_too_vague` | "generation incoherent" | fixable | yes |
| `same_error_3x` | same `wrong_style_output` 3x | persistent | escalate |
| `worker_500_all` | "worker returned 500" on every worker | catastrophic | no |
| `fixable_then_pass` | fixable once, remanifest, then pass | fixable + remanifest | yes |
| `recovery_log_integrity` | any case | recovery_log appended exactly once per decision | — |

### Simulators

`ESCALATION_ACTOR_SIMULATOR` (only for the cases where classifier asks
for more diagnostic context, which can happen via multi-turn). Optional
for this component.

### Thresholds

- `ContractComplianceEvaluator` (both) = 1.00 (hard)
- `EscalationDecisionEvaluator` ≥ 0.70 (soft)
- `TrajectoryEvaluator` ≥ 0.70 (soft)
- `OutputEvaluator(_REMANIFEST_RUBRIC)` ≥ 0.75 (soft)

---

## File layout

```
server/strands_agents/
├── recovery_agents.py                            # ~350 LOC (both agents)
└── hooks/
    └── recovery_logger.py                        # ~60 LOC
```

---

## Acceptance criteria

- [ ] Classifier deterministic on identical errors (temperature = 0).
- [ ] Remanifest preserves required fields for all fixable cases.
- [ ] `recovery_log` has exactly one entry per decision.
- [ ] Persistent case triggers `escalation_request`.
- [ ] Experiment passes thresholds.
