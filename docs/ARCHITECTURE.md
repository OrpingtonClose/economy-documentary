# Architecture

## Overview

The documentary pipeline is a **SequentialAgent** orchestration built on
Google ADK (Agent Development Kit), served via FastAPI with CopilotKit AG-UI
for the frontend.

```
┌─────────────────────────────────────────────────────┐
│                    Frontend (Next.js)                │
│  CopilotKit Chat │ Dashboard │ Gates │ Timeline View │
└──────────────────┬──────────────────────────────────┘
                   │ AG-UI Protocol (SSE)
┌──────────────────┴──────────────────────────────────┐
│                 FastAPI Server                        │
│  AG-UI Endpoint │ Dashboard SSE │ Health Check        │
└──────────────────┬──────────────────────────────────┘
                   │
┌──────────────────┴──────────────────────────────────┐
│            Master SequentialAgent                     │
│                                                      │
│  1. Scenario Director (EvaluatorOptimizer)           │
│  2. Audio Agent (TTS + WhisperX)                     │
│  3. Visual Director (LoopAgent × 3 sub-agents)       │
│  4. Production Supervisor (GPU orchestration)         │
│  5. Assembler Agent (ffmpeg assembly)                │
│                                                      │
│  ┌─────────────────────────────────────────────┐     │
│  │  Shared State (Blackboard Pattern)          │     │
│  │  topic, scenes, whisperx_alignment,         │     │
│  │  content_analysis, visual_concepts,         │     │
│  │  coherence_evaluation, otio_mutations       │     │
│  └─────────────────────────────────────────────┘     │
│                                                      │
│  ┌─────────────────────────────────────────────┐     │
│  │  Timeline Guardian (after_agent_callback)   │     │
│  │  Validates OTIO timeline after each phase   │     │
│  └─────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────┘
```

## Data Flow

1. **Scenario Director**: Topic → SCENARIO JSON with V1/V2/V3 voice blocks
2. **Audio Agent**: Scene scripts → WAV files + WhisperX word-level timing
3. **Visual Director**: WhisperX timing → Content analysis → Visual concepts
4. **Production Supervisor**: Visual concepts → GPU-generated video clips
5. **Assembler Agent**: OTIO timeline → Trimmed/muxed/concatenated final MP4

## ADK Patterns Used

### EvaluatorOptimizer (Scenario Director)
Generator creates content, evaluator checks quality. Loop until GOOD rating.

### LoopAgent (Visual Director)
Three sub-agents iterate: Content Analyst → Visual Concepter → Coherence
Evaluator. Loops until coherence rating ≥ GOOD.

### SequentialAgent (Master Pipeline)
Agents execute in strict order. State flows via session blackboard.

## Plugin Stack

- **ContextFilterPlugin**: Manages context window by keeping only recent tool results
- **ReflectAndRetryToolPlugin**: Auto-retries failed tool calls (max 2)
- **GlobalInstructionPlugin**: Injects documentary-specific instructions
- **LoggingPlugin / DebugLoggingPlugin**: Observability

## Callback Stack

- **before_model**: LLM concurrency semaphore + context-length safety
- **after_model**: Dashboard tracking
- **before_tool**: Per-provider rate limiting + dashboard tracking
- **after_tool**: Result truncation + dashboard tracking
- **timeline_guardian**: Phase-specific OTIO validation (after_agent)

## OTIO Timeline

The pipeline uses OpenTimelineIO as its single source of truth for media
assembly. Three tracks:

- **V1_Video**: Video clips (LTX-2.3 generated)
- **A1_Narration**: Narration audio clips (Qwen3-TTS generated)
- **A2_Music**: Background music clips

All timeline operations include idempotency checks to prevent duplicate clips.

## Dashboard

Real-time pipeline instrumentation with:
- SQLite-backed event store (WAL mode for concurrent reads)
- SSE streaming for live updates
- Self-contained HTML reports for post-mortem analysis
- Per-run collectors with async context isolation

## Media Immutability Invariant

Once a piece of media (video clip, narration audio, music track, rendered
scene) has been created, it is **immutable**. The only permitted
operations on existing media are:

- **Replace** — swap it for a freshly generated alternative (full
  regeneration of the same slot).
- **Extend** — append additional newly-generated media (e.g. a short
  extension clip to fill remaining narration time).

The following are **forbidden** at all layers (recovery, escalation,
assembly):

- **Looping** — repeating existing media to fill time.
- **Time-stretching** — speeding up or slowing down existing media to
  fit a target duration.
- **Frozen frames / freeze-frame padding** — holding a single frame
  (or any static image derived from existing media) to extend visible
  duration is forbidden. This applies whether the freeze occurs at the
  start, middle, or end of a clip, and regardless of the mechanism
  (decoder hold, still-image insert, last-frame repeat, etc.). If a
  slot needs more visual duration, generate a new clip — do not hold
  a frame.

This invariant is the reason the canonical escalation menu offers
`generate_extension_clip` (extend) and `regenerate_clip` / `rewrite_scene`
(replace), but not duration-fitting via stretch or loop. Any recovery
action or assembly step that would violate this invariant must fail
closed and escalate rather than silently degrade.

## Escalation Pattern

Every pipeline operation is wrapped by the recovery middleware
(`server/recovery.py`). The intended design is a **graduated ladder of
LLM-powered agents**, one per rung, with increasing authority and scope.

### Intended ladder (L0 – L4)

| Level | Name | Role |
|------:|------|------|
| **L0** | FIX | Domain specialist agent rewrites inputs to fix the specific problem (e.g. `AudioTimingAgent` rewrites narration to fix timing; `VisualPromptAgent` rewrites a visual prompt from QA feedback). |
| **L1** | RETRY | Intelligent retry agent analyses error patterns and adjusts params. Not dumb exponential backoff. |
| **L2** | CREATIVE | Alternative-strategy agent brainstorms a different model / different approach. |
| **L3** | COLLABORATIVE | Inter-agent agent talks to other pipeline agents to coordinate a fix. |
| **L4** | HUMAN | AG-UI escalation, full diagnostic chain presented. Last resort. |

Every agent returns a `RecoveryDecision`:

```python
action ∈ {"fix", "retry", "skip", "escalate", "abort"}
state_patches: dict         # mutations to apply to op kwargs before retry
explanation: str
confidence: float
```

`fix` / `retry` re-run the operation (with `state_patches` applied on
`fix`); `skip` accepts the failure; `escalate` advances to the next rung;
`abort` stops the pipeline.

Core types: `RecoveryLevel` (IntEnum 0–4), `RecoveryPolicy`,
`RecoveryAgent` base class + domain agents in `recovery_agents.py`
(`AudioTimingAgent`, `VisualPromptAgent`, `ProductionBatchAgent`,
`OTIOValidationAgent`, `RetryAgent`, `CreativeAgent`,
`CollaborativeAgent`). Registries wire `{L0..L3 → agent}` via
`AUDIO_AGENTS` / `VIDEO_AGENTS` / `PRODUCTION_AGENTS` / `OTIO_AGENTS` /
`GENERIC_AGENTS`. Factory policies: `_make_audio_agent_policy()`,
`_make_video_agent_policy()`, `_make_production_agent_policy()`,
`_make_otio_agent_policy()`, `_make_generic_agent_policy()` (level
budgets default to `{0: 3-5, 1: 3, 2: 2, 3: 1}`).

Entry point:

```python
execute_with_recovery(
    operation, operation_name, kwargs, policy,
    context=None, pipeline_state=None, diagnostic_data=None,
)
```

Dispatch in `recovery.py`:

- `policy.agents` set → `_execute_with_agents()` (the intended L0–L3 ladder)
- `policy.agents` missing → `_execute_legacy()` (retry + callback
  amendments + `EnvironmentalAssessor`, kept for backward compat)

### Canonical action menu (supervisor layer)

When a recovery consult reaches the Production Supervisor
(`agents/production_supervisor.py::supervisor_escalate`), it MUST return
exactly one `EscalationAction` from the canonical menu defined in
`orchestrator/escalation_menu.py`:

| Tier | Action | Purpose |
|-----|--------|---------|
| L1 | `regenerate_clip(clip_id, prompt_delta, seed_delta)` | Cheap, targeted retry with corrective guidance and/or seed perturbation (replace). |
| L1 | `generate_extension_clip(scene_id, duration_needed)` | Fill remaining narration time with a short newly-generated clip (extend; 0.5–3.0s typical). |
| L2 | `trim_narration(scene_id, max_cut_sec)` | Cut up to `max_cut_sec` seconds off the end of narration. |
| L2 | `freeze_frame_fill(scene_id, duration_needed)` | (Deprecated — violates Media Immutability Invariant; retained for backward compat, should not be selected.) |
| L2 | `replace_with_brand_card(scene_id)` | Static brand/title card in place of the scene (replace). Heavy narrative cost. |
| L3 | `rewrite_scene(scene_id, guidance)` | Regenerate narration + visual brief via scenario director (replace). |
| L3 | `abort_run(reason)` | Stop the pipeline. Last resort. |

Decision rule: pick the cheapest tier that resolves the failure while
preserving narrative. Prefer L1 > L2 > L3. Every tier is either a
replace or an extend — never a stretch, loop, or freeze. Signatures
are enforced in `EscalationAction.__post_init__`.

> **Note:** `speed_up_narration` has been removed from the canonical
> menu in accordance with the Media Immutability Invariant above.
> `freeze_frame_fill` also violates the invariant and is deprecated;
> it remains defined for backward compatibility but must not be
> selected by the supervisor.

### Hard invariant

`assert_escalation_invariant(escalations_per_run, llm_calls_per_run)`
is checked at end-of-run: any run that had at least one escalation MUST
have made at least one supervisor LLM call. A violation means the
pipeline fell back to round-robin with zero reasoning (the exact
regression that issues #61, #73, #102 close).

### Current status vs intent

The ladder, agents, registries, and policy factories are implemented,
but the production hot paths still call the **legacy policies**
(`VIDEO_POLICY`, `TTS_POLICY`) in `tools/video_tools.py` and
`tools/tts_tools.py`. Those policies have `agents=None`, so every GPU
clip and every TTS call dispatches to `_execute_legacy` — the intended
L0–L3 agent ladder is not exercised on the hot path today. The
supervisor is reached via `_consult_supervisor` from inside the legacy
path, meaning the supervisor is currently a bolted-on late consultant
rather than a rung of the ladder. Migrating the hot-path policies to
the agent factories (`_make_video_agent_policy`, `_make_audio_agent_policy`)
is the outstanding work to bring reality in line with intent.
