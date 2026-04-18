# Architecture

## Overview

The documentary pipeline is a Google ADK SequentialAgent orchestration
served via FastAPI with a CopilotKit AG-UI frontend. The frontend
communicates with the backend over the AG-UI protocol on Server-Sent
Events. The backend exposes the AG-UI endpoint, a dashboard SSE
stream, and a health check.

The master SequentialAgent runs five stages in strict order: Scenario
Director (an EvaluatorOptimizer that loops until the scenario rates
GOOD), Audio Agent (text-to-speech plus WhisperX word-level
alignment), Visual Director (a LoopAgent of Content Analyst, Visual
Concepter, and Coherence Evaluator that iterates until coherence rates
at least GOOD), Production Supervisor (GPU orchestration of clip
generation), and Assembler Agent (ffmpeg-based final assembly).

State flows between stages through a shared session blackboard holding
topic, scenes, WhisperX alignment, content analysis, visual concepts,
coherence evaluation, and OTIO mutations. After every stage the
Timeline Guardian runs as an after-agent callback and validates the
OTIO timeline.

## Data Flow

The Scenario Director turns a topic into a scenario JSON of V1, V2,
and V3 voice blocks. The Audio Agent renders each block as a WAV
file and WhisperX produces word-level timing. The Visual Director
reads that timing, runs content analysis, and emits visual concepts
that match it. The Production Supervisor generates a video clip per
visual concept on GPU workers. The Assembler Agent walks the OTIO
timeline and ffmpeg-trims, muxes, and concatenates the final MP4.

## ADK Patterns

The Scenario Director uses an EvaluatorOptimizer: a generator agent
produces content and an evaluator agent grades it; the loop runs until
the rating is GOOD. The Visual Director uses a LoopAgent of three
sub-agents (Content Analyst, Visual Concepter, Coherence Evaluator)
that iterates until coherence is at least GOOD. The master pipeline
is a SequentialAgent so stages execute strictly in order with state
passed through the blackboard.

## Plugin Stack

The pipeline runs with four ADK plugins. ContextFilterPlugin manages
the LLM context window by keeping only recent tool results.
ReflectAndRetryToolPlugin auto-retries failed tool calls up to two
times. GlobalInstructionPlugin injects documentary-specific
instructions on every model call. LoggingPlugin and
DebugLoggingPlugin provide observability.

## Callback Stack

Five callback layers wrap every agent. before_model enforces an LLM
concurrency semaphore and a context-length safety check. after_model
records dashboard tracking. before_tool enforces per-provider rate
limits and dashboard tracking. after_tool truncates results and
records dashboard tracking. timeline_guardian runs as an
after_agent callback and performs phase-specific OTIO validation.

## OTIO Timeline

The pipeline uses OpenTimelineIO as the single source of truth for
media assembly. The timeline has three tracks: V1_Video for video
clips (LTX-2.3 generated), A1_Narration for narration audio (Qwen3-TTS
generated), and A2_Music for background music. Every timeline
operation is idempotent so resumed runs cannot create duplicate clips.

## Dashboard

The dashboard provides real-time pipeline instrumentation backed by a
SQLite event store in WAL mode for concurrent reads. SSE streaming
delivers live updates to the frontend, and self-contained HTML reports
are generated for post-mortem analysis. Per-run collectors are
isolated by async context.

## Media Immutability Invariant

Once a piece of media — a video clip, narration audio, music track,
or rendered scene — has been created, it is immutable. Only two
operations on existing media are permitted: replace (swap it for a
freshly generated alternative, regenerating the entire slot) and
extend (append additional newly-generated media, for example a short
extension clip to fill remaining narration time).

The following are forbidden at every layer of the pipeline (recovery,
escalation, and assembly): looping (repeating existing media to fill
time), time-stretching (speeding up or slowing down existing media to
fit a target duration), and frozen frames in any form (holding a
single frame, or any static image derived from existing media, to
extend visible duration). The freeze prohibition applies whether the
hold occurs at the start, middle, or end of a clip and regardless of
the mechanism (decoder hold, still-image insert, last-frame repeat).
If a slot needs more visual duration, generate a new clip; do not
hold a frame.

This invariant is the reason the canonical escalation menu offers
generate_extension_clip (extend) and regenerate_clip / rewrite_scene
(replace), but not duration-fitting via stretch, loop, or freeze. Any
recovery action or assembly step that would violate this invariant
must fail closed and escalate rather than silently degrade.

## Escalation Pattern

Every pipeline operation is wrapped by the recovery middleware in
server/recovery.py. The intended design is a graduated ladder of
LLM-powered agents, one per rung, with increasing authority and
scope.

The ladder has five rungs. L0 (FIX) is a domain specialist agent that
rewrites inputs to fix the specific problem; for example, the audio
timing agent rewrites narration to fix duration overruns and the
visual prompt agent rewrites a visual prompt from QA feedback. L1
(RETRY) is an intelligent retry agent that analyses error patterns
and adjusts parameters; it is not dumb exponential backoff. L2
(CREATIVE) is an alternative-strategy agent that brainstorms a
different model or different approach. L3 (COLLABORATIVE) is an
inter-agent agent that talks to other pipeline agents to coordinate a
fix. L4 (HUMAN) is the AG-UI escalation with the full diagnostic
chain presented; it is the last resort.

Every recovery agent returns a decision with an action (one of fix,
retry, skip, escalate, abort), a set of state patches to apply to the
operation kwargs before retry, an explanation, and a confidence
score. Fix and retry re-run the operation (with state patches applied
on fix); skip accepts the failure and moves on; escalate advances to
the next rung; abort stops the pipeline.

When recovery escalates to the Production Supervisor, the supervisor
must return exactly one canonical EscalationAction. There are six
canonical actions across three tiers. At L1 the supervisor may pick
regenerate_clip (cheap, targeted retry of a single clip with
corrective guidance and an optional seed perturbation; this is a
replace) or generate_extension_clip (fill remaining narration time
with a short newly-generated clip, typically 0.5 to 3.0 seconds; this
is an extend). At L2 the supervisor may pick trim_narration (cut up
to a bounded number of seconds off the end of narration) or
replace_with_brand_card (substitute a static brand or title card for
the scene; this is a replace with heavy narrative cost). At L3 the
supervisor may pick rewrite_scene (regenerate the scene's narration
and visual brief through the scenario director; this is a replace) or
abort_run (stop the pipeline; last resort).

The decision rule is to pick the cheapest tier that resolves the
failure while preserving narrative. Prefer L1 over L2 over L3. Every
tier is either a replace or an extend; no tier is ever a stretch, a
loop, or a freeze. The supervisor's choice is signature-validated
before execution.

speed_up_narration and freeze_frame_fill were once part of the
canonical menu and have been removed because both violate the Media
Immutability Invariant. Narration pauses are now rendered as black
gaps in the assembly pipeline; the assembler never holds a last
frame.

A hard invariant is checked at the end of every run: any run that
performed at least one escalation must also have made at least one
supervisor LLM call. A violation means the pipeline fell back to
round-robin recovery with zero reasoning, which is the regression
that this design exists to prevent.
