# LTX-Video 2.3 tuning

Knowledge page. Updated as we accumulate tuning data from real runs.

## VRAM footprint (placeholder — fill in after slice 5 first run)

- Declared minimum: 48 GB VRAM (enforced by the playground pre-flight probe;
  see `preflight_vram()` in `server/strands_agents/playground/worker_registry.py`).
- Starting class for first runs: H200 (141 GB) — deliberately
  overprovisioned to confirm end-to-end behaviour before downshifting.
- Target downshift: L40S (48 GB) or A100 (40 GB) after 3+ successful
  runs with observed peak VRAM logged in
  [`../gpu-sizing.md`](../gpu-sizing.md).

## Disk footprint

- Weights: ~55 GB (estimate, confirm after first run)
- Intermediate scene mp4s: ~15 GB per 60 s documentary
- Recommend: 500 GB on first VM, downshift after observation.

## Known failure modes

<!-- Fill in after first real runs -->
