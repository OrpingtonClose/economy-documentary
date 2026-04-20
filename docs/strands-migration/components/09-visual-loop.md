# 09 — visual-loop (DeepAgent `SubAgent`)

The visual loop is a **cohesive domain with its own isolated context**:
content analysis, visual concept generation, and coherence evaluation
iterate against each other before anything is launched for GPU
production. The right deepagents primitive is a
[`SubAgent`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py#L25).

The orchestrator delegates to `visual` via the built-in `task` tool,
receives a summary + handoff artifacts, and carries on to production.

---

## Intent

Given the accepted scenes + style_lock + audio + timing report, produce:

- Per-scene **content analysis** (component 06): phrase classification,
  camera language hints, visual beats.
- Per-scene **visual concepts** (component 07): up to N candidate
  prompts per scene, style-locked.
- A **coherence verdict** (component 08): do the concepts cohere across
  scenes? If not, which scenes need revision?

Internal loop (inside the SubAgent's own planning):

1. `analyze_content_phrases(scenes=...)` (one-shot per scene set).
2. For each scene, `generate_visual_concepts(scene=..., analysis=..., style_lock=...)`.
3. `evaluate_visual_coherence(concepts_by_scene=...)` → verdict.
4. If not `GOOD`+, revisit worst-scoring scenes via
   `generate_visual_concepts` with the coherence feedback; then re-run
   step 3.
5. Bounded at 5 iterations — hard stop returns the best-scoring set.

Returns to the orchestrator: `concepts_by_scene` (dict), `coherence_verdict`
(`EXCELLENT` / `GOOD` / `FAIR` / `POOR`), `per_scene_report`.

---

## Current implementation

`server/agents/visual_director.py` — 830 lines. A `LoopAgent` with three
sub-agents (content_analyst, visual_concepter, coherence_evaluator) plus
three approval-gate callbacks. The composition is not readable in any
single file.

---

## Target implementation

### SubAgent declaration

```python
# server/strands_agents/subagents/visual.py
from deepagents.types import SubAgent

VISUAL_SUBAGENT_PROMPT = """\
You are the visual production planner for a documentary pipeline.

Given the accepted scenes (scenes.json) + style_lock + audio alignment,
produce per-scene visual concepts that cohere across the whole piece.

Process:
1. Call analyze_content_phrases once with the full scenes list.
2. For each scene, call generate_visual_concepts with scene, analysis,
   and style_lock.
3. Call evaluate_visual_coherence across all scenes.
4. If the verdict is FAIR or POOR, identify the lowest-scoring scenes
   and regenerate concepts for those scenes only with the coherence
   feedback included. Re-evaluate.
5. Stop when verdict is GOOD or EXCELLENT, or after 5 iterations.

You MUST NOT call any launch_* tool. Production dispatch is handled by
the parent orchestrator.
"""

visual_subagent: SubAgent = {
    "name": "visual",
    "description": (
        "Visual production planner. Invoke with the accepted scenes + "
        "style_lock + timing_report. Returns concepts_by_scene + "
        "coherence_verdict."
    ),
    "system_prompt": VISUAL_SUBAGENT_PROMPT,
    "tools": [
        analyze_content_phrases,         # component 06 leaf (as @tool)
        generate_visual_concepts,        # component 07 leaf (as @tool)
        evaluate_visual_coherence,       # component 08 leaf (as @tool)
        read_file,                       # inherited filesystem
        write_file,
    ],
    "model": os.environ.get("STRANDS_THINKER_MODEL", "openai/gpt-4o"),
    "middleware": [
        SummarizationMiddleware(max_tokens_before_summary=10_000),
    ],
}
```

### Orchestrator invocation

```python
# in the main DeepAgent's planning
task(
    subagent_type="visual",
    description=(
        "Generate visual concepts for the 5 accepted scenes. Style_lock "
        "and scenes.json are already on disk."
    ),
)
```

The main orchestrator reads back `concepts_by_scene.json` from the
shared filesystem after the `task` tool returns the SubAgent's summary.

### Why a SubAgent instead of flat tools on the main orchestrator?

1. **Context isolation.** The visual loop easily generates thousands of
   tokens per iteration. Keeping it in the main orchestrator's context
   displaces the planning state we need for production dispatch.
2. **Cohesive domain.** The three leaves (analyst, concepter,
   coherence) only make sense together.
3. **Model choice.** The visual loop benefits from `STRANDS_THINKER_MODEL`
   (often a different / larger model than the orchestrator uses for
   cheap tool orchestration).
4. **Observability.** Langfuse gets a clean nested trace for the whole
   visual stage.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    VisualCoherenceEvaluator(),               # custom — LLM-as-judge
    ToolSelectionAccuracyEvaluator(),         # analyst → concepter → coherence sequence
    VisualLoopTrajectoryEvaluator(),          # bounded iteration, regeneration only for weak scenes
    ContractComplianceEvaluator(VISUAL_CONTRACT),
]
```

### Test cases (minimum 5)

| Case | Scenes | Seeded behaviour | Expected outcome |
|------|--------|------------------|------------------|
| `one_shot_good` | 3 | Concepter returns cohesive concepts | 1 iteration, verdict GOOD |
| `one_revise` | 5 | Scene 3 concept off-style | 2 iterations, scene 3 revised |
| `persistent_fair` | 5 | Revision doesn't fix | Stops at 5 iterations, returns best |
| `analyst_fails` | 3 | `analyze_content_phrases` raises | SubAgent reports error to parent, no concepts written |
| `style_lock_drift` | 4 | Concepter introduces off-style elements | Coherence catches drift, revision fixes |

### Simulators

None at the SubAgent level (real leaves run). Fixture `style_lock` and
fixture scenes JSON drive behaviour.

### Thresholds

| Evaluator | Min score | Hard gate |
|-----------|-----------|-----------|
| `VisualCoherenceEvaluator` | 0.70 | No |
| `ToolSelectionAccuracyEvaluator` | 0.70 | No |
| `VisualLoopTrajectoryEvaluator` | 0.80 | Yes |
| `ContractComplianceEvaluator` | 1.00 | Yes |

---

## File layout

```
server/strands_agents/
├── subagents/
│   └── visual.py                 # SubAgent declaration (< 100 LOC)
└── evals/
    └── experiments/
        └── visual_experiment.json
```

---

## Acceptance criteria

- [ ] `visual` SubAgent shipped as a `SubAgent` TypedDict consumed by
      `create_deep_agent(subagents=[...])`.
- [ ] Leaves 06, 07, 08 imported as `@tool`s on the SubAgent.
- [ ] System prompt forbids `launch_*` calls from within the SubAgent.
- [ ] All 5 cases pass thresholds.
- [ ] Langfuse trace shows the nested `task` span containing the
      SubAgent's tool calls.
