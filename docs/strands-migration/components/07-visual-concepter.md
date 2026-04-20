# 07 — visual-concepter

LLM agent that turns the Content Analyst's phrases into LTX-2.3 shot
prompts. Enforces `style_lock` so the documentary doesn't drift into
visual whiplash between scenes.

---

## Intent

Given `content_analysis`, `style_lock`, and `visual_style`, produce
`visual_concepts: list[dict]` (one per phrase). Each concept specifies
camera movement, shot type, LTX-2.3 prompt, negative prompt,
duration, and params. See
[`STATE_SCHEMA.md § 9`](../contracts/STATE_SCHEMA.md#9-visual_concepts-listdict).

---

## Current implementation

Second sub-agent in
[`server/agents/visual_director.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/agents/visual_director.py)
(~lines 310-540). Prompt specifies shot-type vocabulary, camera-move
vocabulary, and the rule "never propose a shot inconsistent with
`style_lock.dominant_style`".

---

## Strands implementation

Single file: `server/strands_agents/visual_concepter.py`.

### System prompt

~800 tokens. Covers shot-type vocabulary, camera-move vocabulary, prompt
template, negative-prompt defaults, duration mapping from phrase
`time_span`. The full LTX-2.3 parameter reference is loaded via a skill
(same pattern as 01's scenario schema).

### Tools (3)

```python
@tool
def propose_concept(
    phrase: dict, style_lock: dict, visual_style: dict,
) -> dict:
    """Produce one visual concept for one phrase. Returns a visual_concepts[] entry."""

@tool
def check_style_lock(
    concepts: list[dict], style_lock: dict,
) -> dict:
    """Verify every concept matches dominant_style + palette. Returns {ok, violations: [...]}"""

@tool(context=True)
async def persist_visual_concepts(context, visual_concepts: list[dict]) -> dict:
    """Write to invocation_state and return."""
```

### Hooks

```python
hooks = [
    ContractEnforcer(VISUAL_DIRECTION_CONTRACT),
    StyleLockEnforcer(),                 # AfterToolCallEvent: reject concepts violating style_lock
    RevisionTagger(artifact_type="visual_concepts"),
]
```

`StyleLockEnforcer` is a small `HookProvider` that listens to
`AfterToolCallEvent` for `propose_concept`, inspects the result, and sets
`event.retry = True` with a user-visible feedback string if the concept
violates `style_lock` (forbidden style, wrong palette, disallowed
realism anchor). Prevents drift without relying on the LLM noticing.

### Conversation manager

`SlidingWindowConversationManager(window_size=40)`. Visual-concept
generation fans out to ~3-5 phrases per scene × N scenes; 40 covers
reasonable phrase counts.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(VISUAL_DIRECTION_CONTRACT),
    VisualCoherenceEvaluator(),
    ToolSelectionAccuracyEvaluator(),
    CoherenceEvaluator(),
]
```

### Test cases (minimum 5)

| Case name | Input | Expected |
|-----------|-------|----------|
| `cinematic_doc` | style_lock.dominant_style=cinematic_documentary | every concept uses real-world camera moves; no animation |
| `hand_drawn` | style_lock.dominant_style=hand_drawn_animation | concepts reference 2D, ink, linework; no 3D |
| `realism_anchor` | style_lock.realism_anchors=["4K","no CGI"] | negative_prompt includes "CGI, cartoon" |
| `forbidden_style` | style_lock.forbidden_styles=["anime"] | retry path fires; final concepts free of anime cues |
| `phrase_data_heavy` | phrase_type=data | concept selects a shot_type suited to data-vis (graph, stat overlay) |

### Thresholds

- `ContractComplianceEvaluator` = 1.00 (hard)
- `VisualCoherenceEvaluator` ≥ 0.70 (soft)
- `ToolSelectionAccuracyEvaluator` ≥ 0.70 (soft)

---

## File layout

```
server/strands_agents/
├── visual_concepter.py                          # ~250 LOC
└── hooks/
    └── style_lock_enforcer.py                   # ~80 LOC
```

---

## Acceptance criteria

- [ ] One concept per phrase for all non-failure cases.
- [ ] `StyleLockEnforcer` retry fires on `forbidden_style` — observable in OTel trace.
- [ ] No concept's `style_lock_applied` is `False`.
- [ ] Experiment passes thresholds.
