# 02 — timing-evaluator

**Not an agent.** A single deterministic `@tool` function.

---

## Intent

Given the OTIO timeline and `whisperx_alignment`, compute whether the
total audio duration and per-scene durations are within tolerance of
their targets. Set `timing_passed: bool` and emit a structured
`timing_report` for downstream nodes (the refiner in component 03, the
cycle edge in component 05).

---

## Current implementation

[`server/agents/timing_evaluator.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/agents/timing_evaluator.py)
lines 51-238. Currently exposed as an ADK `Agent` just so it fits into
a `LoopAgent` — but the logic is entirely deterministic. No LLM calls.

---

## Strands implementation

Single module: `server/strands_agents/timing_tool.py`. One function:

```python
from strands import tool

_DEFAULT_TOLERANCE = 0.10   # ±10% on total
_PER_SCENE_TOLERANCE = 0.15 # ±15% per scene

@tool(context=True)
async def evaluate_timing(
    context,
    scenes: list[dict],
    whisperx_alignment: dict,
    target_duration_sec: float,
    tolerance: float = _DEFAULT_TOLERANCE,
) -> dict:
    """Compare actual narration duration to target; fail if outside tolerance."""
    total_actual = whisperx_alignment["total_duration_sec"]
    deviation = (total_actual - target_duration_sec) / target_duration_sec

    per_scene: list[dict] = []
    for scene, seg in zip(scenes, whisperx_alignment["per_scene"]):
        target = float(scene["target_duration_sec"])
        actual = float(seg["duration_sec"])
        scene_dev = (actual - target) / target
        per_scene.append({
            "scene_id": scene["id"],
            "target": target,
            "actual": actual,
            "deviation": scene_dev,
            "ok": abs(scene_dev) <= _PER_SCENE_TOLERANCE,
        })

    violations: list[str] = []
    if abs(deviation) > tolerance:
        violations.append(f"total duration off by {deviation:+.1%}")
    violations.extend(
        f"scene {s['scene_id']} off by {s['deviation']:+.1%}"
        for s in per_scene if not s["ok"]
    )

    report = {
        "target_duration_sec": target_duration_sec,
        "actual_duration_sec": total_actual,
        "deviation_ratio": deviation,
        "per_scene_analysis": per_scene,
        "violations": violations,
    }
    timing_passed = not violations

    context.invocation_state["timing_passed"] = timing_passed
    context.invocation_state["timing_report"] = report
    return {"timing_passed": timing_passed, "timing_report": report}
```

All prior constants, tolerance logic, and per-scene breakdown live here,
ported verbatim.

### Why a tool, not an agent

Logic is deterministic and side-effect-free. Wrapping it in an `Agent`
would:

- Cost an LLM call per cycle of the timing loop.
- Introduce non-determinism into a correctness check.
- Require a prompt for something that a 30-line function does.

The timing-loop (component 05) invokes this tool either directly (via a
wrapper agent with just this one tool) or as a
`strands.multiagent.AgentBase` wrapping the tool call; the choice depends
on whether we want graph-level routing on `timing_passed`. See component
05.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(TIMING_CONTRACT),
    Equals("timing_passed", expected),   # deterministic per-case
]
```

### Test cases (minimum 6)

| Case name | Input alignment total / target | Expected `timing_passed` |
|-----------|--------------------------------|--------------------------|
| `under_tolerance_exact` | 300 / 300 | True |
| `over_tolerance_total` | 360 / 300 (+20%) | False |
| `under_tolerance_total` | 240 / 300 (−20%) | False |
| `within_per_scene_but_total_off` | scenes all ok, total off by 11% | False |
| `per_scene_spike` | total ok, one scene +20% | False |
| `edge_zero_target` | 10 / 0 | raises `ValueError` (test via `pytest.raises`) |

### Simulators

None. Pure function over structured inputs.

### Thresholds

- `ContractComplianceEvaluator` ≥ 1.00 (hard)
- `Equals("timing_passed", expected)` = 1.00 per case (hard)

---

## File layout

```
server/strands_agents/
├── timing_tool.py                   # < 80 LOC
└── evals/
    ├── evaluators/
    │   └── contract_compliance_evaluator.py (shared, lands with 01)
    └── experiments/
        └── timing_experiment.json
```

---

## Acceptance criteria

- [ ] `evaluate_timing` unit-tested with the 6 cases above.
- [ ] Experiment loads + runs via `Experiment.from_file`.
- [ ] Same-input determinism verified (run twice, identical `timing_report`).
- [ ] No LLM call in traces.
