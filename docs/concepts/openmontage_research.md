> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# OpenMontage Research Notes

## What It Is
- First open-source, agentic video production system
- 12 pipelines, 57+ tools, 500+ agent skills
- LLM coding assistant IS the orchestrator (no separate runtime)
- Uses Remotion (React/Node.js) for final video composition
- Total cost per video: $0.15–$1.33

## Architecture (Key for Us)
```
User idea
  |
  v
Agent reads pipeline manifest (YAML)
  |
  v
For each stage:
  1. Read stage-director skill (Markdown)
  2. Call Python tools via registry
  3. Write checkpoint (JSON) with artifacts
  4. Self-review using meta/reviewer skill
  5. Human approval gate (optional)
  |
  v
Final video output
```

## Their Repository Layout
- `lib/` — core runtime (config, checkpoint, pipeline loader, media profiles)
- `tools/` — 57+ Python tools (audio, video, graphics, analysis, enhancement)
- `pipeline_defs/` — 11 YAML pipeline manifests
- `skills/` — agent instructions per pipeline stage
- `styles/` — visual style playbooks
- `remotion-composer/` — Node.js/React video renderer
- `schemas/` — JSON schemas for validation

## Relevant Pipelines for Us
1. **documentary-montage.yaml** — "Corpus-building documentary from free archives"
   - Research phase: build corpus from free stock footage and open archives
   - Retrieval: fetch actual motion clips
   - Editing: stitch into timeline
   - Rendering: final composition
2. **cinematic.yaml** — Cinematic trailers with motion clips
3. **hybrid.yaml** — Mix of generated + stock footage

## Key Insight: Agent IS the Control Plane
- No Python orchestrator runtime
- Agent reads manifest, follows skills, calls tools, checkpoints state
- Human only intervenes at approval gates
- This is exactly what we want for our provisioner-agent model

## Cost Model
- They use API-based providers (fal.ai, OpenAI, etc.)
- Our model uses Vast.ai GPU rental + DeepSeek API
- Our cost per scene: ~$0.15–$0.50 (GPU rental) + API calls
- Their cost per video: $0.15–$1.33 total

## What We Can Steal
1. Pipeline manifest pattern (YAML stage definitions)
2. Checkpoint/artifact pattern (JSON state between stages)
3. Stage-director skill pattern (Markdown instructions per stage)
4. Tool registry pattern (auto-discover Python tools)
5. Self-review meta-skill (agent reviews own output)

## What We Do Differently
1. We use Vast.ai GPU workers instead of API providers
2. We use B2 for artifact storage instead of local checkpoints
3. We use DeepSeek reasoning models instead of Claude
4. Our workers are ephemeral VMs, not persistent services
5. Our provisioner agent manages infrastructure, not just creative
