# 05 — timing-loop (orchestration plan)

**Not a composition.** The timing loop is a trajectory the orchestrator
follows, using the leaves in components 02–04. No new code ships in this
PR; what ships is the **spec** (this document), the
`TimingLoopTrajectoryEvaluator`, and the `Experiment` that pins the
trajectory.

---

## Intent

Given `scenes`, `target_duration_sec`, and any upstream state, the
orchestrator must:

1. Launch per-scene audio tasks in parallel via `launch_audio_render`.
2. `await_tasks` on the batch.
3. Call `evaluate_timing` on the combined WhisperX alignment.
4. If `timing_passed=False`:
   1. Call `refine_scenario` with the `timing_report`.
   2. `write_file("scenes.json", refined)`.
   3. Go back to step 1.
5. If `timing_passed=True`: stop the loop, return control to the main
   orchestrator plan (next stage is visual production, component 09).

Bounded at **10 iterations** (matches the current
`TIMING_LOOP_MAX_ITERATIONS`). The bound is enforced by a planning
heuristic in [`AGENTS.md`](../AGENTS.md), not by a graph construct.

---

## Current implementation

`server/agents/pipeline.py` lines ~139–171 assemble a
`LoopAgent("timing_loop", max_iterations=10, sub_agents=[audio_agent, timing_evaluator, scenario_refiner])`.
Loop exits when `scenario_refiner` sees `state["timing_passed"] is True`
and returns a cancelling content block via `_skip_if_timing_passed`.

---

## Target implementation

There is no `timing_loop.py` module. The loop is encoded as:

1. The orchestrator's system prompt — short paragraph explaining the
   timing stage.
2. [`AGENTS.md`](../AGENTS.md) heuristics:
   - "Launch every scene's `launch_audio_render` in one turn, then
     `await_tasks` on the batch."
   - "After `evaluate_timing` returns `timing_passed=False`, call
     `refine_scenario` at most once before re-launching audio."
   - "Hard-stop the loop at 10 iterations; if still failing, delegate
     to the `escalation` SubAgent."
3. Evals that pin the above trajectory (see below).

### Expected tool-call trajectory (happy path, 3 scenes, 2 iterations)

```
launch_audio_render(scene_id="s1", ...)     # iteration 1
launch_audio_render(scene_id="s2", ...)
launch_audio_render(scene_id="s3", ...)
await_tasks(task_ids=[t1, t2, t3])
evaluate_timing(scenes=..., whisperx_alignment=..., intent_target_sec=...)
# → timing_passed=False, scene s2 over by 4s

refine_scenario(scenes=..., timing_report=...)
write_file("scenes.json", refined_scenes_json)

launch_audio_render(scene_id="s1", ...)     # iteration 2
launch_audio_render(scene_id="s2", ...)
launch_audio_render(scene_id="s3", ...)
await_tasks(task_ids=[t4, t5, t6])
evaluate_timing(...)
# → timing_passed=True, exit loop
```

### Anti-patterns the evals catch

- Serial audio launches (`await_tasks` after each individual
  `launch_audio_render`) — wastes time.
- Calling `refine_scenario` without a `timing_report` input — loses the
  per-scene diagnostic that tells the refiner what to adjust.
- Calling `evaluate_timing` before `await_tasks` on the launched batch —
  runs on stale alignment.
- Calling `refine_scenario` twice back-to-back without a fresh
  `evaluate_timing` in between.
- Exceeding 10 iterations without delegating to `escalation`.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    TimingLoopTrajectoryEvaluator(),        # orchestration trajectory (see below)
    ToolSelectionAccuracyEvaluator(),       # expected tools called
    ParallelLaunchEvaluator(tool="launch_audio_render"),
    ContractComplianceEvaluator(TIMING_CONTRACT),
    Equals("timing_passed", True),          # end state
]
```

`TimingLoopTrajectoryEvaluator` (custom — specified in
[`eval-framework/CUSTOM_EVALUATORS.md`](../eval-framework/CUSTOM_EVALUATORS.md))
checks the tool-call sequence matches the expected pattern:

1. All `launch_audio_render` calls in each iteration must happen before
   the matching `await_tasks`.
2. At most one `refine_scenario` call per iteration.
3. `evaluate_timing` called exactly once per iteration, after
   `await_tasks`.
4. Iteration count ≤ 10.

`ParallelLaunchEvaluator` checks that `launch_audio_render` calls within
each iteration are emitted in the same turn (i.e. the orchestrator used
one `tool_calls` batch, not N sequential turns).

### Test cases (minimum 5)

| Case | Scenes | Seeded alignment behaviour | Expected trajectory |
|------|--------|----------------------------|---------------------|
| `one_shot_pass` | 3 | First render is within ±2 s | 1 iteration, no refiner |
| `one_refine_pass` | 5 | First render off by +6 s, refined render within ±2 s | 2 iterations, 1 refine |
| `per_scene_spike` | 5 | Total ok, 1 scene over by 20 % | 2 iterations, refiner adjusts that scene only |
| `refiner_no_op` | 3 | Refiner returns identical scenes (edge bug) | Iteration cap triggers escalation delegation |
| `max_iterations` | 5 | Every iteration still off | Stops at 10, delegates to `escalation` |

### Simulators

- `TTSToolSimulator` (component 04) drives `launch_audio_render` and
  `await_tasks` to return seeded WhisperX alignments.
- No refiner simulator — the real refiner Strands agent runs (component
  03).

### Thresholds

| Evaluator | Min score | Hard gate |
|-----------|-----------|-----------|
| `TimingLoopTrajectoryEvaluator` | 0.90 | Yes |
| `ToolSelectionAccuracyEvaluator` | 0.85 | Yes |
| `ParallelLaunchEvaluator` | 0.80 | No |
| `ContractComplianceEvaluator` | 1.00 | Yes |
| `Equals("timing_passed", True)` (where applicable) | 1.00 | Yes |

---

## Acceptance criteria

- [ ] No new module. The loop exists as a planning trajectory.
- [ ] `AGENTS.md` contains the heuristics above under the **Timing stage**
      section.
- [ ] `TimingLoopTrajectoryEvaluator` implemented and used by the
      experiment.
- [ ] All 5 cases pass thresholds.
- [ ] Max-iterations case correctly delegates to the `escalation`
      SubAgent (component 13) with a structured diagnostic payload.
