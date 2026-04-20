# 09 — visual-loop

Second `GraphBuilder` composition. Combines 06 → 07 → 08 with a cycle
edge from 08 → 07 when coherence fails.

---

## Intent

```
content_analyst → visual_concepter → coherence_evaluator
                                       │
                                       └── if not visual_coherence_passed ──► visual_concepter
```

Bounded at 5 iterations (matches current `LoopAgent(max_iterations=5)`).

---

## Current implementation

`server/agents/visual_director.py` — the outer `LoopAgent` that contains
content_analyst + visual_concepter + coherence_evaluator.

---

## Strands implementation

Single module: `server/strands_agents/visual_loop.py`.

```python
from strands.multiagent import Graph, GraphBuilder
from .content_analyst import build_content_analyst
from .visual_concepter import build_visual_concepter
from .coherence_evaluator import build_coherence_evaluator

def build_visual_loop() -> Graph:
    return (
        GraphBuilder()
        .set_graph_id("visual_loop")
        .add_node(build_content_analyst(), node_id="content_analyst")
        .add_node(build_visual_concepter(), node_id="visual_concepter")
        .add_node(build_coherence_evaluator(), node_id="coherence_evaluator")
        .add_edge("content_analyst", "visual_concepter")
        .add_edge("visual_concepter", "coherence_evaluator")
        .add_edge(
            "coherence_evaluator", "visual_concepter",
            condition=lambda s: not s.results["coherence_evaluator"].result.output.get(
                "visual_coherence_passed", False,
            ),
        )
        .set_entry_point("content_analyst")
        .set_max_node_executions(15)    # 5 iterations * 3 nodes
        .set_hook_providers([ContractEnforcer(VISUAL_DIRECTION_CONTRACT)])
        .build()
    )
```

### Why no content_analyst in the cycle

The content analyst's output is anchored to the audio timing — it does
not need to re-run when the visual concepter has to tweak concepts. The
cycle is narrowly around concepter ↔ evaluator. This matches the current
`LoopAgent` behaviour (content_analyst only runs once per loop entry)
but is now explicit in the graph shape.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(VISUAL_DIRECTION_CONTRACT),
    TrajectoryEvaluator(),
    Equals("visual_coherence_passed", True),    # for success cases
    VisualCoherenceEvaluator(),
]
```

### Test cases (minimum 5)

| Case name | Initial state | Expected node sequence | Expected final `visual_coherence_passed` |
|-----------|---------------|------------------------|------------------------------------------|
| `one_shot_pass` | clean content_analysis, coherence clears first try | `ca → vc → ce` | True |
| `one_refinement_pass` | concepter overshoots style_lock on first try | `ca → vc → ce → vc → ce` | True |
| `two_refinements_pass` | repeated style drift | 7-node trace | True |
| `never_converges` | synthetic unsolvable case | hits `max_node_executions` | False |
| `coherence_hard_fail` | style_lock violation rating POOR | cycle fires until fixed or bound hit | True or False (depending on case) |

### Thresholds

- `ContractComplianceEvaluator` = 1.00 (hard)
- `TrajectoryEvaluator` ≥ 0.75 (soft)
- `VisualCoherenceEvaluator` ≥ 0.70 (soft)

---

## File layout

```
server/strands_agents/visual_loop.py            # ~120 LOC
```

---

## Acceptance criteria

- [ ] Cycle termination proven via `never_converges` — graph exits cleanly.
- [ ] Content analyst runs exactly once per graph invocation (not per cycle).
- [ ] OTel trace visualisable in Phoenix with correct cycle edges.
- [ ] Component 14 can compose this graph as a sub-graph node.
