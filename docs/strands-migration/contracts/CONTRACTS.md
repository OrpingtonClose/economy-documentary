# CONTRACTS — stage contracts in Strands

`server/contracts.py` already exists and is correct. The rewrite keeps the
`StageContract` dataclass and every concrete contract (`SCENARIO_CONTRACT`,
`AUDIO_CONTRACT`, `VISUAL_DIRECTION_CONTRACT`, `PRODUCTION_CONTRACT`,
`ASSEMBLY_CONTRACT`) **as-is** — they express correctness properties the
pipeline must preserve regardless of framework.

What changes is **how** contracts are enforced.

---

## 1. The contract module we keep

Verbatim from the current repo (no changes expected):

- `StyleLock` — one style family for the whole documentary
  ([`contracts.py:88`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/contracts.py#L88)).
- `HookSpec`, `OutroSpec`, `scene_pronunciation_hints`, etc. — structural
  invariants on `scenes[]`.
- `ServiceRequirement(name, env_var, capability, required)` — worker
  health declarations.
- `StageContract(name, required_services, required_state, produced_state, produced_artifacts)`.
- `ContractViolation(RuntimeError)` with `stage` + `details` dict.
- `_PLACEHOLDER_VALUES` frozenset (`""`, `"[]"`, `"(not yet analyzed)"`, …).

`validate_preconditions(contract, state)` and
`validate_postconditions(contract, state, output_dir)` in `contracts.py`
stay. The move is in the *enforcement layer* that calls them.

---

## 2. Enforcement in ADK (current)

In `server/callbacks/*.py` the pattern is:

```python
# BEFORE
def before_agent_callback(callback_context):
    validate_preconditions(SCENARIO_CONTRACT, callback_context.state)
```

attached by `pipeline.py` via monkey-patching:

```python
# pipeline.py ~lines 181–230
_orig_before = scenario_director.before_agent_callback
scenario_director.before_agent_callback = make_composite_callback([
    _orig_before, _preflight_gate_before, make_preflight_validator(SCENARIO_CONTRACT),
])
```

This is the source of the 1 111-line `pipeline.py` — wrapping and unwrapping
callbacks to compose enforcement, seeding, preview triggers, etc.

---

## 3. Enforcement in Strands (target)

A single `HookProvider` per contract, in `server/strands_agents/hooks/contracts.py`:

```python
from dataclasses import dataclass
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeInvocationEvent, AfterInvocationEvent
from contracts import StageContract, validate_preconditions, validate_postconditions

@dataclass
class ContractEnforcer(HookProvider):
    """Validates a StageContract's pre- and postconditions around an agent invocation."""

    contract: StageContract
    output_dir: str = "/tmp/documentary-pipeline"

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeInvocationEvent, self._before)
        registry.add_callback(AfterInvocationEvent, self._after)

    def _before(self, event: BeforeInvocationEvent) -> None:
        state = event.agent.state.as_dict() | (event.invocation_state or {})
        validate_preconditions(self.contract, state)      # raises ContractViolation

    def _after(self, event: AfterInvocationEvent) -> None:
        state = event.agent.state.as_dict() | (event.invocation_state or {})
        validate_postconditions(self.contract, state, self.output_dir)
```

Every component attaches its contract enforcer in its own file:

```python
# server/strands_agents/scenario_agent.py
from strands import Agent
from contracts import SCENARIO_CONTRACT
from .hooks.contracts import ContractEnforcer

agent = Agent(
    model=...,
    tools=[...],
    hooks=[ContractEnforcer(SCENARIO_CONTRACT)],
    system_prompt=...,
    conversation_manager=SlidingWindowConversationManager(window_size=20),
)
```

No monkey-patching. No composite callbacks. The contract is visible at the
call site, which means reviewers can spot missing enforcement by reading
one file.

---

## 4. Contract in `GraphBuilder`

A graph may also enforce a contract at node boundaries (for example,
"production_supervisor may not run unless `whisperx_alignment` is real").
Strands supports this via hooks on the graph itself:

```python
graph = (
    GraphBuilder()
    .add_node(scenario_agent, node_id="scenario")
    .add_node(production_supervisor, node_id="production")
    .set_hook_providers([
        ContractEnforcer(PRODUCTION_CONTRACT),  # validates around the whole graph run
    ])
    .build()
)
```

For per-node contract enforcement, register a hook that listens to
`BeforeNodeCallEvent` / `AfterNodeCallEvent` and dispatches on `node_id`.
The canonical implementation is in
[`STRANDS_SDK_PATTERNS.md`](../reference/STRANDS_SDK_PATTERNS.md).

---

## 5. Why contracts survive the rewrite

The invariants `server/contracts.py` enforces are lessons the pipeline
paid for in production (PAG-run duration shortfall, visual whiplash,
"(not yet generated)" placeholders leaking downstream, worker going
degraded mid-run). Deleting any of them re-opens the regression. Keep:

- **One-model-per-VM assumption**: services must report their
  `capability` in `/health` (never run inference on the wrong VM).
- **Real-values assertion**: `_PLACEHOLDER_VALUES` never satisfies a
  `required_state` key.
- **Artifact-existence assertion**: `produced_artifacts` globs must match
  at least one file on disk before declaring the stage done.
- **Service-health pre-check**: `required_services` must pass health
  before the stage starts.

---

## 6. New contracts worth introducing post-migration

Optional, listed here for completeness:

- `TIMING_CONTRACT` — codifies the timing-loop's postcondition
  (`timing_passed == True` and `total_duration` within tolerance).
  Currently implicit in `timing_evaluator.py`.
- `RECOVERY_CONTRACT` — each recovery agent must either produce a fixed
  artifact or surface an `escalate` decision. No silent no-ops.
- `ESCALATION_CONTRACT` — supervisor run must terminate (abort, fix, or
  resume). No hanging on interrupts beyond the session TTL.

Open these as separate PRs after the migration, once we have three
nightly runs of baseline data to set numeric tolerances.
