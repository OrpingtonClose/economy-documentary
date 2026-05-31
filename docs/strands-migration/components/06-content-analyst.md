> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# 06 — content-analyst

First of four components in the visual pipeline. Parses narration into
"phrases" (narrative beats) anchored to WhisperX timestamps, which the
visual concepter (07) then turns into shot prompts.

---

## Intent

Given `scenes`, `whisperx_alignment`, produce `content_analysis: dict`
with per-scene phrase breakdowns. Every phrase has:

- A stable `phrase_id` (hash of scene_id + word_span).
- A `phrase_type` classification.
- A `narrative_weight` (hook / build / payoff / connective).
- A `visual_intent` string (~1 sentence).
- `word_span` and `time_span` tying it back to the audio.

See [`STATE_SCHEMA.md § 8`](../contracts/STATE_SCHEMA.md#8-content_analysis-dict)
for the exact shape.

---

## Current implementation

Lives inside `server/agents/visual_director.py` (830 lines) as the first
of three sub-agents in a `LoopAgent`. The content analyst's role is
buried ~lines 120-310 there. The "virtual brief" that it produces is
used by
[`server/callbacks/virtual_brief.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/callbacks/virtual_brief.py).

---

## Strands implementation

Single file: `server/strands_agents/content_analyst.py`.

### System prompt

~400 tokens. Covers:

- Role ("you are the Content Analyst for a documentary pipeline").
- Output contract: call `extract_phrases` once per scene; when every
  scene has been analysed, call `persist_content_analysis` and return.
- Phrase-type vocabulary (concept / entity / process / transition / data).
- Narrative-weight vocabulary (hook / build / payoff / connective) — at
  least one `hook` on scene 1, at least one `payoff` on the final scene.
- Do not split across scenes — phrases are scene-local.

### Tools (3)

```python
@tool
def extract_phrases(
    scene: dict, whisperx_segment: dict, max_phrases: int = 6,
) -> dict:
    """Parse one scene's narration into phrases. Returns {"scene_id": int, "phrases": [...]}"""

@tool
def validate_phrases(content_analysis: dict) -> dict:
    """Check phrase_type, narrative_weight, time_span coverage. Returns {ok, issues: [...]}"""

@tool(context=True)
async def persist_content_analysis(context, content_analysis: dict) -> dict:
    """Write to invocation_state and return."""
```

### Hooks

```python
hooks = [
    ContractEnforcer(VISUAL_DIRECTION_CONTRACT),
    RevisionTagger(artifact_type="content_analysis"),
]
```

### Conversation manager

`SlidingWindowConversationManager(window_size=30)` — scales with scene
count; at ~5 messages per scene, 30 comfortably covers a 15-scene
documentary.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(VISUAL_DIRECTION_CONTRACT),
    TrajectoryEvaluator(),
    OutputEvaluator(rubric=_CONTENT_ANALYSIS_RUBRIC),
    FaithfulnessEvaluator(),       # phrases must be grounded in scene text
]
```

`_CONTENT_ANALYSIS_RUBRIC` (short): "every scene has at least one phrase;
every phrase has a valid `phrase_type`; scene 1 contains a `hook`-weighted
phrase; final scene contains a `payoff`; `time_span` values are
monotonically non-decreasing".

### Test cases (minimum 5)

| Case name | Input | Expected |
|-----------|-------|----------|
| `standard_5_scenes` | 5 scenes, full alignment | each scene 3-5 phrases; hook on s1, payoff on s5 |
| `data_heavy_scene` | scene with numeric narration | ≥ 1 `data`-typed phrase |
| `short_scene_10s` | 10 s scene | ≤ 2 phrases; still has a narrative_weight |
| `multi_voice_scene` | V1+V2+V3 block | phrases span all three speakers |
| `missing_alignment` | scene without whisperx segment | tool raises or validator flags issue |

### Thresholds

- `ContractComplianceEvaluator` = 1.00 (hard)
- `TrajectoryEvaluator` ≥ 0.70 (soft)
- `OutputEvaluator` ≥ 0.75 (soft)
- `FaithfulnessEvaluator` ≥ 0.80 (hard — hallucinated visual claims block downstream)

---

## File layout

```
server/strands_agents/content_analyst.py        # ~200 LOC
```

---

## Acceptance criteria

- [ ] Every scene in output has ≥ 1 phrase for all non-failure cases.
- [ ] `phrase_id`s stable across re-runs with identical input.
- [ ] `time_span` values lie within `whisperx_segment`'s duration.
- [ ] Experiment passes thresholds.
