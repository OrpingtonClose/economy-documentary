---
name: recovery-strategies
description: Recovery strategies when video or TTS generation fails
---

# Recovery Strategies

## Video Generation Failures

When a video clip fails quality checks or generation errors:

### 1. Prompt Amendment
- Simplify the prompt — remove complex elements
- Focus on a single strong visual instead of multiple elements
- Add explicit realism anchors from visual_style
- Remove any human figures and replace with objects/landscapes

### 2. Seed Variation
- Try 3 different random seeds before giving up
- Some seeds produce significantly better results for the same prompt
- Track which seeds worked for similar prompts

### 3. Inference Step Adjustment
- Default: 50 steps for quality
- If generation fails: try 30 steps (faster, sometimes avoids artifacts)
- If quality is poor: try 75 steps (slower, more refined)

### 4. LoRA Weight Adjustment
- If style is too strong: reduce weight by 0.1
- If style is too weak: increase weight by 0.1
- Range: 0.3 (subtle) to 0.9 (dominant)

## TTS Generation Failures

### 1. Text Simplification
- Shorten sentences — split long sentences into two
- Replace complex words with simpler alternatives
- Remove parenthetical clauses

### 2. Chunk Splitting
- If a voice block is too long, split into smaller chunks
- Generate each chunk separately, then concatenate
- Maintain natural pause points at split boundaries

## Escalation Order

1. Retry with same parameters (transient errors)
2. Creative amendment (prompt/text modification)
3. Environment assessment (check worker health)
4. Human escalation (operator intervention required)

Never silently degrade — every recovery attempt must be logged.
