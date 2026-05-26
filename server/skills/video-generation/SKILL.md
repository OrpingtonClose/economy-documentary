---
name: video-generation
description: Generate documentary video clips using LTX-2.3 and similar diffusion models — prompt engineering, motion control, and quality optimization
version: 1.0.0
tags:
  - video
  - ltx
  - diffusion
  - prompts
  - documentary
author: pipeline
---

# Video Generation Skill

You are an expert in text-to-video generation for documentary production. This skill gives you deep knowledge of LTX-2.3 and diffusion-based video models.

## LTX-2.3 Technical Profile

**Architecture:** Latent diffusion transformer with 3D spatiotemporal attention
**Optimal Duration:** 4-8 seconds per clip (quality degrades beyond 10s)
**Resolution:** 512×320 minimum; 720×480 for broadcast quality
**VRAM Requirements:** 48GB+ for 720p; 24GB minimum for 512×320
**Frame Rate:** 24fps native; 30fps acceptable

**Prompt Engineering Formula:**
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

**Troubleshooting Failed Renders:**
- "Frozen frames" → Add explicit camera motion keywords
- "Too short / OOM" → Reduce duration to 4s or simplify prompt
- "Color bleed" → Specify lighting: "soft diffused light", "golden hour"
- "Repeated patterns" → Add "unique", "varied", "natural irregularity"

## Self-Directed Research

If you encounter rendering failures or need to optimize:
- Use `RESEARCH: <query>` for quick technical answers
- Use `RESEARCH_DEEP: <query>` for comprehensive guides on prompt techniques
- Use `RESEARCH_NEWS: <query>` for latest model versions, LoRAs, or community findings

Valuable research topics:
- New motion LoRAs released for LTX
- Optimal negative prompts for documentary realism
- Community benchmarks on VRAM vs. quality tradeoffs
