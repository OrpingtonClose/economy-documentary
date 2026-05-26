---
name: video-generation
description: Generate documentary video clips using LTX-2.3 — prompt engineering, parameter tuning, motion control, and resource-aware quality optimization
version: 1.1.0
tags:
  - video
  - ltx
  - diffusion
  - prompts
  - documentary
  - parameters
author: pipeline
---

# Video Generation Skill

You are an expert in text-to-video generation for documentary production. This skill gives you deep knowledge of LTX-2.3 parameters, their resource implications, and how to tune them for quality vs. cost tradeoffs.

## LTX-2.3 Technical Profile

**Architecture:** Latent diffusion transformer with 3D spatiotemporal attention (22B parameters, dev checkpoint)
**Optimal Duration:** 4-8 seconds per clip (quality degrades beyond 10s)
**Resolution:** 512×320 minimum; 720×480 for broadcast quality
**Frame Rate:** 24fps native; 30fps acceptable
**Base Checkpoint:** `ltx-2.3-22b-dev.safetensors`
**Text Encoder:** Gemma-3-12B (non-gated re-host by Lightricks)

## Grid Constraints (Hard Requirements)

LTX-2.3 silently rounds inputs. You must understand these constraints to predict actual output:

- **Width & Height:** Must be multiples of 32. The engine rounds DOWN to the nearest multiple of 32 to avoid silent OOM from rounding up.
  - Request 512×320 → actual 512×320 ✅
  - Request 530×340 → actual 512×320 ⚠️ (rounded down)
  - Request 720×480 → actual 704×480 or 720×480 (720 is 32×22.5, so rounds to 704)

- **Frame Count:** Must be `8k + 1` where k ≥ 1. The engine rounds DOWN:
  - Request 5s @ 24fps = 120 frames → rounds to `((120-1)//8)*8+1` = 113 frames = 4.7s
  - Request 8s @ 24fps = 192 frames → rounds to 185 frames = 7.7s
  - Minimum: 9 frames (k=1)

**Duration Formula:**
```python
raw_frames = int(duration_sec * fps)
num_frames = max(9, ((raw_frames - 1) // 8) * 8 + 1)
actual_duration = num_frames / fps
```

## Parameter Deep Dive

### 1. `num_inference_steps` (Denosing Steps)

| Value | Quality | Speed | Use Case |
|-------|---------|-------|----------|
| 5 | Low (distilled) | ~30-60s on H200 | Fast previews, testing prompts |
| 20 | Medium | ~90-150s | Balanced production work |
| 30 | High | ~120-180s | Default for `run_ltx_2_3.py` |
| 50 | Very High | ~200-300s | Final polish, short clips only |

**VRAM Impact:** Each step is a full forward pass through the 22B model. More steps = linearly more VRAM-time, but peak VRAM is constant (the model weights dominate).

### 2. Guider Parameters (MultiModalGuiderParams)

The LTX-2.3 pipeline uses separate guiders for video and audio (audio guider is unused in our pipeline but configured):

**Video Guider (what matters for us):**
```python
video_guider = MultiModalGuiderParams(
    cfg_scale=3.0,      # Classifier-free guidance scale
    stg_scale=1.0,      # Spatial-temporal guidance
    rescale_scale=0.7,  # Rescale CFG to prevent over-saturation
    modality_scale=3.0, # Cross-modal alignment strength
    skip_step=0,        # Steps to skip guidance (0 = apply all steps)
    stg_blocks=[28],    # Which transformer blocks get STG
)
```

**Tuning Guidance:**
- `cfg_scale` (1.0–7.0): Higher = stronger prompt adherence but can cause artifacting or "burned" colors. Default 3.0 is balanced.
- `stg_scale` (0.5–2.0): Higher = sharper motion but may introduce jitter. Keep at 1.0 unless motion is specifically problematic.
- `rescale_scale` (0.0–1.0): Lower = more saturated/vivid; higher = more conservative. 0.7 prevents CFG over-saturation.
- `modality_scale` (1.0–5.0): Higher = stronger text-to-visual alignment. 3.0 is safe; increase to 4.0-5.0 if prompt is being ignored.

**VRAM Impact:** Guidance parameters do NOT affect peak VRAM (computed during inference, not model loading). They affect quality only.

### 3. Resolution & Frame Count → VRAM Scaling

This is the most critical resource table. Use it to decide what GPU to provision:

| Resolution | Frames (5s@24fps) | Peak VRAM (no offload) | Peak VRAM (cpu offload) | Peak VRAM (fp8-cast) |
|-----------|-------------------|------------------------|-------------------------|----------------------|
| 512×320 | 113 (~4.7s) | ~40 GB | ~24 GB | ~22 GB |
| 512×320 | 185 (~7.7s) | ~48 GB | ~28 GB | ~24 GB |
| 704×480 | 113 | ~55 GB | ~32 GB | ~28 GB |
| 704×480 | 185 | ~72 GB | ~40 GB | ~36 GB |
| 720×480 | 113 | ~58 GB | ~34 GB | ~30 GB |
| 1280×720 | 113 | OOM on single GPU | ~55 GB | ~48 GB |

**Key Insight:** Frame count scales VRAM non-linearly (attention is O(n²) in frames). A 7.7s clip at 512×320 uses ~20% more VRAM than a 4.7s clip at the same resolution.

### 4. Offload Modes

Passed as `--offload` to the LTX-2.3 CLI:

| Mode | Speed | Peak VRAM | Use Case |
|------|-------|-----------|----------|
| `none` (default) | Fastest | Full model in VRAM | H200, H100 80GB, multi-GPU |
| `cpu` | ~2× slower | ~40-60% reduction | A100 40GB, tight budgets |
| `disk` | ~5× slower | ~70% reduction | Emergency only, very slow |

**Recommendation:** Use `cpu` offload when targeting A100 40GB or RTX A6000. Never use `disk` for production — only for testing on underspec'd machines.

### 5. Quantization

Passed as `--quantization` to the LTX-2.3 CLI:

| Mode | Speed Impact | VRAM Reduction | Quality Impact |
|------|-------------|----------------|----------------|
| `fp8-cast` | ~5% faster | ~10-15% | Barely perceptible |
| `fp8-scaled-mm` | ~5% faster | ~10-15% | Slightly more stable numerics |
| None (default) | Baseline | Baseline | Best quality |

**Recommendation:** Use `fp8-cast` on H100/A100 for 512×320 production. Skip quantization on H200 (plenty of VRAM, why risk quality).

### 6. Negative Prompt

The pipeline already appends a strong baseline negative:
```
worst quality, inconsistent motion, blurry, jittery, distorted, static, low resolution, morphing, warping, flicker, text, watermark, logo
```

You may append additional negatives specific to your scene:
- For nature scenes: `artificial, plastic, CGI-looking, oversaturated`
- For realism: `cartoon, anime, illustration, painting`
- For motion: `frozen, still image, no movement, static camera`

## Prompt Engineering Formula

```
[Subject] + [Action/Motion] + [Environment] + [Lighting] + [Camera Movement] + [Style/Mood]
```

**Motion Keywords (critical for avoiding frozen frames):**
- Camera: "slow dolly forward", "gentle push-in", "subtle orbit", "crane up"
- Natural: "wind rustling leaves", "water flowing", "particles drifting", "steam rising"
- Biological: "slow breathing motion", "subtle pulse", "gentle sway"

**Anti-Patterns (avoid these):**
- Static descriptions without motion: "a beautiful mountain" → will be still
- Text or logos in frame: models cannot render readable text
- Multiple human faces: uncanny valley artifacts
- Extreme close-ups of organic textures: macro skin/eye renders poorly
- Fast cuts or scene changes: models generate continuous motion, not edits

## Troubleshooting Failed Renders

| Symptom | Root Cause | Fix |
|---------|-----------|-----|
| "Frozen frames" | Prompt lacks motion keywords | Add explicit camera/natural motion |
| "Too short / OOM" | GPU OOM during generation | Reduce duration to 4s, simplify prompt, or use cpu offload |
| "Color bleed" | Insufficient lighting spec | Add "soft diffused light", "golden hour" |
| "Repeated patterns" | Model hallucinating texture | Add "unique", "varied", "natural irregularity" |
| "Black output" | Worker crash or model pin mismatch | Check `/workspace/agent.log`, verify model weights |

## Resource-Aware Parameter Selection

When planning a clip, choose parameters based on available GPU:

**If VM has H200 (141GB VRAM):**
- Resolution: 704×480 or 720×480
- Duration: 5-8s
- Steps: 30
- Offload: none
- Quantization: none

**If VM has H100 80GB:**
- Resolution: 512×320 or 704×480 (short clips)
- Duration: 4-5s
- Steps: 20-30
- Offload: none for 512×320, cpu for 704×480
- Quantization: fp8-cast for 704×480

**If VM has A100 40GB:**
- Resolution: 512×320 only
- Duration: 4-5s
- Steps: 20
- Offload: cpu
- Quantization: fp8-cast

**If VM has RTX 4090 (24GB):**
- Resolution: 512×320 only
- Duration: 4s max
- Steps: 5-10
- Offload: cpu
- Quantization: fp8-cast
- Expect slower renders; consider upgrading

## Self-Directed Research

If you encounter rendering failures or need to optimize:
- Use `RESEARCH: <query>` for quick technical answers
- Use `RESEARCH_DEEP: <query>` for comprehensive guides on prompt techniques
- Use `RESEARCH_NEWS: <query>` for latest model versions, LoRAs, or community findings

Valuable research topics:
- New motion LoRAs released for LTX
- Optimal negative prompts for documentary realism
- Community benchmarks on VRAM vs. quality tradeoffs
