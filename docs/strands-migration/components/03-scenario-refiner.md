# 03 — scenario-refiner

Small LLM agent. Adjusts `scenes[]` durations and narration pacing to
bring `timing_passed` back to `True`. Conditionally skipped when the
previous timing evaluation passed.

---

## Intent

Take `scenes: list[dict]` + `timing_report: dict`, emit a modified
`scenes: list[dict]` with:

- Per-scene `target_duration_sec` adjusted to match actual audio so the
  scenario narrative intent is preserved but timing is feasible, **or**
- Voice block text trimmed / expanded to hit the target duration.

Never invents new scenes; never drops existing ones. Preserves all
required fields (`pronunciation_hints`, `voices[].voice_id`,
`hook_spec`, `outro_spec`).

---

## Current implementation

[`server/agents/scenario_refiner.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/agents/scenario_refiner.py),
system prompt at lines 113-163. The `_skip_if_timing_passed` callback at
the top of the file short-circuits the agent when `state["timing_passed"]`
is `True`.

---

## Strands implementation

Single file: `server/strands_agents/scenario_refiner.py`.

### System prompt

Port lines 113-163 verbatim. Add at the end:

> The input `scenes` and `timing_report` are in the first user message.
> Call `adjust_scene_durations` or `tweak_voice_text` (one or both), then
> call `validate_pronunciation_hints`, then return the updated scenes via
> `persist_refined_scenes`. Do NOT invent scenes; do NOT delete scenes.

### Tools (3)

```python
@tool
def adjust_scene_durations(
    scenes: list[dict], per_scene_targets: dict[int, float],
) -> dict:
    """Replace target_duration_sec per scene; preserves all other fields."""

@tool
def tweak_voice_text(
    scenes: list[dict], scene_id: int, direction: Literal["shorten", "lengthen"],
    delta_sec: float,
) -> dict:
    """Trim or expand V1/V2/V3 narration blocks in a scene by ~delta_sec of speech."""

@tool
def validate_pronunciation_hints(scenes: list[dict]) -> dict:
    """Verify every voice block retains its pronunciation_hints; returns {ok, missing_on: [...]}."""

@tool(context=True)
async def persist_refined_scenes(context, scenes: list[dict]) -> dict:
    """Write scenes to invocation_state and return."""
```

### Hooks

```python
hooks = [
    ContractEnforcer(SCENARIO_CONTRACT),       # preconditions only on refiner
    SkipIfTimingPassed(),                      # BeforeToolCallEvent → cancel_tool if state["timing_passed"]
    RevisionTagger(artifact_type="scenario"),
]
```

`SkipIfTimingPassed` replaces `_skip_if_timing_passed`. It's a small
`HookProvider` that cancels the first tool call with a cancel message —
the agent then emits the existing scenes unchanged.

### Conversation manager

`SlidingWindowConversationManager(window_size=10)`. Refinement is a
single-pass mechanical job; no long history needed.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(SCENARIO_CONTRACT),
    Contains("pronunciation_hints"),     # deterministic; every scene's JSON contains the key
    OutputEvaluator(rubric=_REFINER_RUBRIC),
    TrajectoryEvaluator(),
]
```

`_REFINER_RUBRIC` (short): "scenes JSON has the same `id` set as the
input; `hook_spec` present on scene 1; `outro_spec` present on final
scene; `voice_id` preserved per voice block; narrative intent
recognizable".

### Test cases (minimum 5)

| Case name | Input | Expected behaviour |
|-----------|-------|--------------------|
| `timing_passed_noop` | `timing_passed=True`, 5 scenes | trajectory is empty after `SkipIfTimingPassed`; scenes unchanged |
| `shorten_single_scene` | scene 3 over by 20% | calls `tweak_voice_text(3, shorten, ~3s)`; scene 3 new text ~20% shorter |
| `lengthen_single_scene` | scene 2 under by 18% | calls `tweak_voice_text(2, lengthen, ~5s)`; scene 2 extended |
| `total_off_per_scene_ok` | total off by 12%, all scenes ok | calls `adjust_scene_durations` proportionally |
| `preserve_pronunciation_hints` | any case with hints | `validate_pronunciation_hints` returns `ok=True` |

### Simulators

None.

### Thresholds

- `ContractComplianceEvaluator` ≥ 1.00 (hard)
- `Contains("pronunciation_hints")` = 1.00 (hard)
- `OutputEvaluator(_REFINER_RUBRIC)` ≥ 0.75 (soft)
- `TrajectoryEvaluator` ≥ 0.70 (soft; allows flexibility in tool order)

---

## File layout

```
server/strands_agents/
├── scenario_refiner.py                       # ~200 LOC
└── hooks/
    └── skip_if_timing_passed.py              # ~30 LOC
```

---

## Acceptance criteria

- [ ] Unit tests on all 4 tools.
- [ ] `SkipIfTimingPassed` test: feed `timing_passed=True`, assert agent emits no tool calls.
- [ ] Scene cardinality invariant: output `len(scenes) == input len(scenes)` across all cases.
- [ ] `pronunciation_hints` preserved on every voice block after refinement.
- [ ] Experiment passes thresholds.
