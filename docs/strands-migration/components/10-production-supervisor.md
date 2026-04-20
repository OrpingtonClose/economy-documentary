# 10 — production-supervisor

The most complex single agent. Dispatches GPU video jobs, monitors them,
triages failures along the escalation ladder, and writes
`clip_artifacts`.

---

## Intent

Given `visual_concepts`, `whisperx_alignment`, and a healthy GPU worker
pool, produce `clip_artifacts: list[dict]` — one `.mp4` per visual
concept, uploaded to B2, QA-gated.

When a job fails:

1. Transient failures: retry (up to 3x).
2. Fixable failures: invoke the diagnostic classifier (12); if the
   classifier says fixable, ask the remanifestation agent (12) for a
   concept tweak; re-dispatch.
3. Persistent failures: escalate to supervisor (13).
4. Catastrophic failures: abort run.

---

## Current implementation

[`server/agents/production_supervisor.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/agents/production_supervisor.py)
(~260 lines) + the escalation ladder implemented in-line with several
callbacks. GPU worker client at
[`server/tools/video_tools.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/tools/video_tools.py).

---

## Strands implementation

Single file: `server/strands_agents/production_supervisor.py`.

### System prompt

~1 200 tokens. Covers:

- Role ("you are the Production Supervisor").
- Dispatch loop: `check_worker_health` → for each concept call
  `dispatch_video_job` → poll `check_job_status` with backoff → on
  success call `run_qa` → on failure call `classify_failure` and route.
- Hard constraints: never skip QA; never silently drop a concept; never
  issue placeholder artifacts; all clips uploaded to B2 before done.
- Escalation ladder: transient (retry up to 3), fixable (remanifest +
  re-dispatch up to 2), persistent (hand to 13), catastrophic (abort).

### Tools (6)

```python
@tool
def check_worker_health() -> dict: ...

@tool
def dispatch_video_job(concept: dict, style_lock: dict) -> dict: ...

@tool
def check_job_status(job_id: str) -> dict: ...

@tool
def run_qa(mp4_path: str, concept: dict) -> dict:
    """Call qa_jury. Returns {"verdict": pass|warn|escalate|fail, "notes": str}."""

@tool
def classify_failure(job_id: str, error: str, context: dict) -> dict:
    """Invoke the diagnostic classifier (12) inline; returns {"class": ..., "hint": ...}."""

@tool(context=True)
async def persist_clip_artifact(context, artifact: dict) -> dict: ...
```

### Hooks

```python
hooks = [
    ContractEnforcer(PRODUCTION_CONTRACT),
    ServiceHealthGate(service="gpu_worker"),        # BeforeInvocationEvent
    TransientRetryPolicy(max_retries=3),            # AfterToolCallEvent on dispatch_video_job/check_job_status
    QaGate(),                                       # AfterToolCallEvent on run_qa: cancel downstream if fail
    EscalationRouter(escalation_supervisor_id=13),  # AfterToolCallEvent on classify_failure
    RevisionTagger(artifact_type="clip"),
]
```

- `ServiceHealthGate` raises `ContractViolation` if `/health` reports
  anything other than `ready` with `capability="video"`.
- `TransientRetryPolicy` uses the same transient-error substrings as
  [STRANDS_SDK_PATTERNS.md §4](../reference/STRANDS_SDK_PATTERNS.md#4-beforeafter-toolcall-hooks-cancel--retry).
- `QaGate` translates `run_qa` verdict into next-tool policy:
  pass → persist, warn → persist + emit warning, escalate → route to
  13, fail → route to 12 for remanifest.
- `EscalationRouter` sets `invocation_state["escalation_request"]`
  visible to component 14's graph edges.

### Conversation manager

`SlidingWindowConversationManager(window_size=60)`. Long-running stage;
each concept takes multiple tool calls; we retain enough history to
reason across concept failures.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(PRODUCTION_CONTRACT),
    TimelineComplianceEvaluator(),
    GoalSuccessRateEvaluator(),
    ToolSelectionAccuracyEvaluator(),
    EscalationDecisionEvaluator(),
    CritiqueStoreEvaluator(artifact_type="clip"),
]
```

### Test cases (minimum 6)

| Case name | GPU simulator behaviour | Expected supervisor behaviour |
|-----------|-------------------------|-------------------------------|
| `clean_dispatch` | all jobs succeed first try | dispatch each concept, QA pass, persist |
| `transient_cuda_oom` | 10% jobs fail transient | retry; all eventually succeed |
| `persistent_checkpoint_error` | 1 job fails 3x on same error | escalate to 13; other concepts complete |
| `fixable_mismatch` | 1 job returns wrong style | classify_failure → fixable; remanifest + re-dispatch; succeeds |
| `catastrophic_worker_down` | all workers return 500 | abort; no partial artifacts persisted |
| `qa_fails_one_clip` | 1 clip QA verdict = fail | trigger remanifest for that clip |

### Simulators

`GPU_WORKER_SIMULATOR` and `ESCALATION_ACTOR_SIMULATOR` from
[`SIMULATION.md`](../eval-framework/SIMULATION.md).

### Thresholds

- `ContractComplianceEvaluator` = 1.00 (hard)
- `TimelineComplianceEvaluator` = 1.00 (hard)
- `GoalSuccessRateEvaluator` ≥ 0.80 (hard)
- `ToolSelectionAccuracyEvaluator` ≥ 0.75 (soft)
- `EscalationDecisionEvaluator` ≥ 0.70 (soft)
- `CritiqueStoreEvaluator` ≥ 0.75 (soft)

---

## File layout

```
server/strands_agents/
├── production_supervisor.py                     # ~400 LOC
└── hooks/
    ├── service_health_gate.py
    ├── transient_retry_policy.py
    ├── qa_gate.py
    └── escalation_router.py
```

---

## Acceptance criteria

- [ ] Every case ends with either all artifacts persisted OR a clean abort (no partial state).
- [ ] `EscalationRouter` emits `escalation_request` exactly once per `persistent_checkpoint_error` case.
- [ ] OTel trace shows the correct tool ladder per case.
- [ ] Each `clip_artifact` has a B2 URL (no local-only paths).
- [ ] Experiment passes thresholds.
