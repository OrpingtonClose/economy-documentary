> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# 08 — coherence-evaluator

LLM-as-judge agent that reviews `visual_concepts` holistically and
decides whether the visual pipeline loop (09) should converge or the
visual concepter (07) should refine.

---

## Intent

Given `visual_concepts`, `style_lock`, `content_analysis`, return a
`visual_coherence_report`:

```python
{
    "rating": "EXCELLENT|GOOD|FAIR|POOR",
    "issues": list[str],
    "suggestions": list[str],
    "visual_coherence_passed": bool,  # True iff rating in {GOOD, EXCELLENT}
}
```

---

## Current implementation

Third sub-agent in `visual_director.py` (~lines 540-830). Uses the same
`CritiqueRating` vocabulary as the scenario evaluator.

---

## Strands implementation

Single file: `server/strands_agents/coherence_evaluator.py`.

### System prompt

~600 tokens. Evaluation criteria:

- **Style consistency**: every concept honours `style_lock.dominant_style`.
- **Camera variety**: consecutive concepts don't use identical shot types.
- **Narrative-visual alignment**: concept for a `hook`-weighted phrase is
  attention-grabbing; concept for a `data`-weighted phrase is
  informative.
- **Transitions**: scene-boundary concepts have compatible framing.

Rating mapping:

- EXCELLENT: no issues.
- GOOD: ≤ 2 issues, no style_lock violations.
- FAIR: 3-5 issues OR one style_lock violation.
- POOR: any hard invariant broken (style_lock forbidden style used,
  >3 consecutive identical shots, scene missing visual for a phrase).

### Tools (2)

```python
@tool
def score_visual_coherence(
    visual_concepts: list[dict], style_lock: dict, content_analysis: dict,
) -> dict:
    """Return {rating, issues, suggestions}. No state write."""

@tool(context=True)
async def persist_coherence_report(context, report: dict) -> dict:
    """Write visual_coherence_report and visual_coherence_passed to invocation_state."""
```

### Hooks

```python
hooks = [
    ContractEnforcer(VISUAL_DIRECTION_CONTRACT),
    RevisionTagger(artifact_type="visual_coherence_report"),
]
```

### Conversation manager

`SlidingWindowConversationManager(window_size=10)`. Single-pass
evaluation; small context.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(VISUAL_DIRECTION_CONTRACT),
    OutputEvaluator(rubric=_COHERENCE_RUBRIC),
    CritiqueStoreEvaluator(artifact_type="visual"),
]
```

### Test cases (minimum 5)

| Case name | Input | Expected rating |
|-----------|-------|-----------------|
| `clean_concepts` | 5 concepts, all style-compliant, varied shots | EXCELLENT or GOOD |
| `style_lock_violation` | 1 concept breaks dominant_style | POOR |
| `repetitive_shots` | 4 consecutive identical shot types | FAIR or POOR |
| `missing_visual` | one phrase has no concept | POOR |
| `minor_palette_drift` | 1 concept off-palette | FAIR |

### Thresholds

- `ContractComplianceEvaluator` = 1.00 (hard)
- `OutputEvaluator` ≥ 0.75 (soft)
- `CritiqueStoreEvaluator` ≥ 0.75 (soft)

---

## File layout

```
server/strands_agents/coherence_evaluator.py    # ~180 LOC
```

---

## Acceptance criteria

- [ ] Rating vocabulary matches `CritiqueRating` in `server/critique/record.py`.
- [ ] `visual_coherence_passed` = True iff rating in {GOOD, EXCELLENT}.
- [ ] Deterministic on identical input (temperature = 0).
- [ ] Experiment passes thresholds.
