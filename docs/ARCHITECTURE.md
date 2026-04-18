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
Generator creates content, evaluator grades it. Loop until GOOD rating.

### LoopAgent (Visual Director)
Three sub-agents iterate: Content Analyst → Visual Concepter → Coherence
Evaluator. Loops until coherence rating ≥ GOOD.

### SequentialAgent (Master Pipeline)
Agents execute in strict order. State flows via session blackboard.

## Plugin Stack

- **ContextFilterPlugin**: Keeps only recent tool results in the LLM context window
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

OpenTimelineIO is the single source of truth for media assembly. Three tracks:

- **V1_Video**: Video clips (LTX-2.3 generated)
- **A1_Narration**: Narration audio clips (Qwen3-TTS generated)
- **A2_Music**: Background music clips

All timeline operations are idempotent. Gaps render as black; never as
held frames.

## GPU Fleet

Video generation (LTX-2.3) and TTS (Qwen3) run on remote GPU workers
provisioned on demand (Vast.ai). Workers require a VRAM floor of
48–80GB depending on the model tier. The fleet coordinator dispatches
work by observed worker health, never by inference from job outcomes;
a single failed clip does not condemn a worker, and a single good clip
does not exonerate one. Workers are provisioned lazily in the
background during CPU-bound script phases so the pipeline is never
idle waiting on boot. Models are loaded sequentially through a
state-dict registry to keep VRAM spikes bounded.

## Dashboard

Real-time pipeline instrumentation with:

- SQLite-backed event store (WAL mode for concurrent reads)
- SSE streaming for live updates
- Self-contained HTML reports for post-mortem analysis
- Per-run collectors with async context isolation

## Human Gates

Every stage boundary emits an approval gate to the AG-UI dashboard
with a 10-second intervention window during which a reviewer may
halt. Once a gate is approved, the approval is binding: downstream
stages may not silently re-run or invalidate approved upstream
artifacts; they may only escalate fresh.

## Quality Gates

Generated clips pass through a two-pass visual QA (Bearnaise
pattern): a structural pass checks technical validity (duration,
codec, resolution, anti-cheat — frozen-frame and temporal-stretch
detection), and a semantic pass checks that the clip matches its
visual concept. Narration passes through a timing-accuracy gate
comparing WhisperX-aligned duration against the OTIO slot.
Cross-scene coherence is checked by the Visual Director's Coherence
Evaluator loop. If any gate is unreachable, ambiguous, or errors,
the pipeline treats that as a failure and escalates; it never
defaults to pass.

## Scripting Rules

Scenarios must comply with the ADHD rule set: open with a hook,
avoid rhetorical questions, keep single-sentence voice blocks at or
below a fixed word budget, and structure each scene around a single
claim. Each scene decomposes into voice blocks (V1 anchor, V2
elaboration, V3 stinger) and the audio durations of those blocks
plus their trailing silences define the video slots. A visual
phrase is the cinematography paragraph for one semantic narration
unit; there is one visual phrase per voice block.

## Media Immutability Invariant

Once a piece of media — a video clip, narration audio, music track,
or rendered scene — has been created, it is immutable. Only two
operations on existing media are permitted: replace (swap it for a
freshly generated alternative, regenerating the entire slot) and
extend (append additional newly-generated media, for example a
short extension clip to fill remaining narration time).

The following are forbidden at every layer of the pipeline
(recovery, escalation, and assembly): looping (repeating existing
media to fill time), time-stretching (speeding up or slowing down
existing media to fit a target duration), and frozen frames in any
form (holding a single frame, or any static image derived from
existing media, to extend visible duration). The freeze prohibition
applies whether the hold occurs at the start, middle, or end of a
clip and regardless of the mechanism (decoder hold, still-image
insert, last-frame repeat). If a slot needs more visual duration,
generate a new clip; do not hold a frame.

## Escalation Pattern

Every pipeline operation is wrapped by the recovery middleware in
server/recovery.py. The intended design is a graduated ladder of
LLM-powered agents, one per rung, with increasing authority and
scope.

The ladder has five rungs. L0 is FIX: a domain specialist agent
rewrites inputs to repair the specific problem — for example, the
audio timing agent rewrites narration to fix duration overruns and
the visual prompt agent rewrites a visual prompt from QA feedback.
L1 is RETRY: an intelligent retry agent analyses error patterns and
adjusts parameters; it is not dumb exponential backoff. L2 is
CREATIVE: an alternative-strategy agent brainstorms a different
model or a different approach. L3 is COLLABORATIVE: an inter-agent
agent talks to other pipeline agents to coordinate a fix. L4 is
HUMAN: AG-UI escalation with the full diagnostic chain presented;
last resort.

## Pipeline Invariants

The following invariants hold across every stage, recovery path, and
assembly step. A violation of any one is a regression.

**Single Source of Truth.** Every audio sample, video frame, and
music segment in the final render must trace to an OTIO entry.
Side-channel media inserted between stages is forbidden.

**Idempotency.** Every operation that touches persistent state (B2,
OTIO, status files, artifacts) is safe to re-run with identical
inputs. Wall-clock timestamps in artifact IDs and unguarded appends
are forbidden.

**Fail-Closed.** When a QA gate, validator, or external service is
unreachable, ambiguous, or errors out, the pipeline treats that as
failure and escalates. Default-to-pass fallbacks and swallowed
exceptions are forbidden.

**Escalation Has Reasoning.** Every recovery decision carries an
LLM-backed reasoning trace. Round-robin recovery and default
actions selected without consulting the supervisor are regressions.

**Provenance.** Every artifact carries full lineage: scene, agent,
model, seed, prompt revision, worker, stage, attempt number.
Anonymous artifacts are forbidden.

**Narration Is Timing Master.** Video durations, music cues, and
visual concepts derive from WhisperX-aligned narration timing.
Video-first timing, audio retimed to fit video, and music tracks of
independent length are forbidden.

**Budget Is Real.** Every cost-incurring operation checks remaining
budget before starting and aborts cleanly when exceeded. Infinite
retry on paid operations is forbidden.

**Single Writer Per Slot.** Only one agent or worker at a time may
write to a given timeline slot. Parallel regeneration of the same
slot is forbidden.

**Stage Boundary Is Hard.** Each SequentialAgent stage must complete
its contract (including Timeline Guardian validation) before the
next stage starts. Partial-state hand-offs are forbidden.

**Resumability.** Every checkpointed artifact (local plus B2) allows
the pipeline to resume from any prior stage without regenerating
downstream-of-checkpoint work. Implicit RAM-only dependencies and
run-id-coupled paths that change on resume are forbidden.

**No Hidden Tools.** Every tool an LLM agent can invoke is declared
in its tool list, traceable in the dashboard, and gated by
before_tool. Shell-outs from inside prompts and untraceable side
effects are forbidden.
