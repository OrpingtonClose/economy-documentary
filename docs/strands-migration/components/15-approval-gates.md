# 15 — approval-gates

Human-in-the-loop at stage boundaries. Replaces the
`.approval_state.json` polling pattern with Strands `Interrupt`.

---

## Intent

Pause the graph after specified stage boundaries, surface the pending
artifact to the user, resume only when the user approves (or rejects).

Three approval surfaces today:

- **Scenario approval** — after the scenario agent (01).
- **Visual approval** — after the visual loop (09).
- **Assembly approval** — after the assembly tool (11), before final
  upload.

All three use the same mechanism.

---

## Current implementation

[`server/callbacks/approval_gate.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/callbacks/approval_gate.py).
Writes a pending state to `.approval_state.json`, polls for user edits
via filesystem. Works but ties the agent process to the filesystem and
can't span process restarts cleanly.

---

## Strands implementation

Single file: `server/strands_agents/approval_gates.py`.

### `ApprovalGate` HookProvider

```python
from dataclasses import dataclass
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterInvocationEvent
from strands.interrupt import Interrupt, InterruptException
from strands.multiagent.graph import AfterNodeCallEvent

@dataclass
class ApprovalGate(HookProvider):
    """Pause the graph after a node, surface its output for user approval."""

    stage: Literal["scenario", "visual", "assembly"]
    after_node: str                                  # node_id to pause after

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterNodeCallEvent, self._maybe_interrupt)

    def _maybe_interrupt(self, event: AfterNodeCallEvent) -> None:
        if event.node_id != self.after_node:
            return
        if event.invocation_state.get("auto_approve"):
            return

        artifact = self._summarise_output(event.result.output)
        raise InterruptException(Interrupt(
            id=f"approval-{self.stage}",
            name=f"{self.stage}_approval",
            reason={
                "stage": self.stage,
                "artifact": artifact,
                "actions": ["approve", "reject", "edit"],
            },
        ))

    def _summarise_output(self, output: dict) -> dict:
        # Stage-specific: e.g. for scenario, pluck titles + durations
        ...
```

### Resume protocol

Caller resumes the graph with an interrupt response payload. Shape:

```python
# approve
{"interruptResponse": {"interruptId": "approval-scenario",
                       "response": {"decision": "approve"}}}

# reject
{"interruptResponse": {"interruptId": "approval-scenario",
                       "response": {"decision": "reject", "reason": "..."}}}

# edit (user modified the artifact on the dashboard; payload contains new version)
{"interruptResponse": {"interruptId": "approval-scenario",
                       "response": {"decision": "edit",
                                    "new_artifact": {...}}}}
```

On `reject`, the `ApprovalGate` sets `invocation_state["halt_reason"] =
"rejected_at_<stage>"` and relies on the next edge's condition to stop
the flow (edges downstream of the approval point check for
`halt_reason`).

On `edit`, the user-edited artifact is written back to
`invocation_state` (`scenes`, `visual_concepts`, or `final_output`
depending on stage), and the graph continues.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    InteractionsEvaluator(),
    Equals("resume_completes_graph", True),
    OutputEvaluator(rubric=_APPROVAL_ROUND_TRIP_RUBRIC),
]
```

`_APPROVAL_ROUND_TRIP_RUBRIC`: "artifact visible in Interrupt reason
matches expectation; resume payload round-trips correctly; on edit,
downstream nodes see the edited artifact".

### Test cases (minimum 5)

| Case name | User action | Expected |
|-----------|-------------|----------|
| `approve_scenario` | approve | graph continues to timing_loop |
| `reject_scenario` | reject | graph halts; no timing_loop node invoked |
| `edit_scenario_durations` | edit with new per-scene durations | timing_loop sees the edited scenes |
| `auto_approve` | `invocation_state["auto_approve"]=True` | no interrupt raised |
| `resume_after_restart` | process killed after interrupt; restarted; approve | graph completes |

### Simulators

`ApprovalActor` — a small `ActorSimulator` with pre-set decisions per
case.

### Thresholds

- `InteractionsEvaluator` ≥ 0.70 (soft)
- `Equals("resume_completes_graph", True)` = 1.00 per success case

---

## File layout

```
server/strands_agents/approval_gates.py           # ~200 LOC
```

---

## Acceptance criteria

- [ ] Every approval round-trip (approve / reject / edit) tested.
- [ ] `resume_after_restart` passes — proves `FileSessionManager` integration.
- [ ] No filesystem polling anywhere.
- [ ] Legacy `.approval_state.json` path removed from the repo in the same PR.
- [ ] Experiment passes thresholds.
