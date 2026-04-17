---
name: batch-optimization
description: GPU batch optimization for video generation efficiency
---

# Batch Optimization for GPU Video Generation

## LoRA Grouping

Group clips by LoRA style for coherence and efficiency:
- Loading a LoRA takes ~5-10 seconds — minimize switches
- Batch all clips using the same LoRA together
- Order batches so similar LoRAs are adjacent (reduces reload overhead)

## Priority Ordering

1. **Style-reference clips first** — generate the clips that define the visual identity early
2. **Longest clips next** — they take the most time and have the highest failure risk
3. **Short filler clips last** — quick to generate and easy to retry

## Worker Load Balancing

When multiple GPU workers are available:
- Distribute clips evenly across workers
- Keep LoRA groups on the same worker (avoid redundant LoRA loads)
- Reserve one worker for retries/recovery
- Monitor VRAM usage — LTX-2.3 needs 48GB+ for bf16

## Batch Size Guidelines

- Optimal batch: 4-8 clips per LoRA group
- Maximum concurrent generations per worker: 1 (LTX-2.3 uses full VRAM)
- Queue depth: up to 3 pending jobs per worker

## Failure Handling

- If a clip fails, move it to the retry queue
- After 3 failures on the same clip, apply recovery strategies
- Never block the entire batch for a single failure
- Track failure patterns — if a worker fails repeatedly, mark it unhealthy

## VRAM Management

- LTX-2.3 bf16: ~48GB VRAM minimum
- Only VRAM is precious — be generous with RAM and disk
- One model per VM — never share, never swap
- Monitor VRAM with nvidia-smi before each batch
