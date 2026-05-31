> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Architecture

See also [ARCHITECTURE_DIAGRAMS.md](./ARCHITECTURE_DIAGRAMS.md) for the eleven companion flow diagrams (top-level flow, narration reconciliation, Preference Ledger, production stage, escalation ladder, RecoveryDecision shape, critique substrate, human gates, GPU fleet + infra escalation, intermediate preview assemblies, dashboard).

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

## Flow of Work

A run starts when a user submits a prompt through the dashboard. The
prompt lands in a session whose identity governs every artifact path
on disk and in B2, so the same prompt submitted twice produces
distinct, resumable runs.

**1. Scenario.** The Scenario Director turns the prompt into a
scenario: a list of scenes, each decomposed into voice blocks (V1
anchor, V2 elaboration, V3 stinger). A generator proposes; an
evaluator grades; the loop runs until the scenario rates GOOD. The
approved scenario is written to the blackboard and surfaced at an
approval gate. The reviewer may approve, halt, or request a rewrite.

**2. Narration.** The Audio Agent synthesizes every voice block
through TTS and runs WhisperX over the output to extract word-level
timing. **Narration is the pipeline's timing master:** the measured
audio durations plus their trailing silences define the video slots
that every downstream stage is bound to. The OTIO timeline is
populated with narration clips at this point. A second approval gate
reviews the generated audio.

**3. Visual plan.** The Visual Director reads WhisperX timing and
runs a three-agent loop: a Content Analyst extracts the semantic
beats of each voice block, a Visual Concepter writes one visual
phrase (the cinematography paragraph) per beat, and a Coherence
Evaluator checks cross-scene visual consistency. The loop runs until
coherence rates at least GOOD. The approved visual plan is attached
to the OTIO timeline as per-slot visual concepts. A third approval
gate reviews the plan as a storyboard.

**4. Clip production.** The Production Supervisor dispatches video
generation across a fleet of GPU workers. Workers are provisioned
lazily in the background during earlier CPU-bound phases so the
pipeline is never idle waiting on boot. Clips generate in parallel,
one per visual phrase. Each returned clip flows through a two-pass
QA — structural (duration, codec, anti-cheat against frozen or
stretched frames) and semantic (does the clip match its visual
concept). Clips that fail enter the recovery ladder; the main flow
is not blocked on individual recoveries until the batch is complete.
A fourth approval gate reviews the assembled set of clips against
the timeline.

**5. Assembly.** The Assembler Agent walks the approved OTIO
timeline and ffmpeg-trims, muxes, and concatenates the final MP4.
Narration and music tracks are composited against the video track.
Gaps render as black. The final file uploads to B2; the run ends.

## Handoffs and Contracts

Stages never hand partial state across boundaries. Each stage must
complete its contract — produce the expected state keys, populate
the OTIO mutations it owns, and pass Timeline Guardian validation —
before the next stage starts. If the contract fails, the stage
escalates rather than hand off degraded state.

The blackboard is the only cross-stage communication channel inside
a run. OTIO is the only cross-stage media contract: every sample of
audio and every frame of video in the final render traces to an
OTIO entry.

## Parallelism

Inside a stage, the pipeline parallelises aggressively: GPU workers
run concurrently, critique agents run fire-and-forget alongside the
main flow, worker provisioning overlaps with CPU-bound script work.
Between stages, the pipeline is strictly sequential; a downstream
stage never starts on partial upstream output.

## Recovery In-Line

Every pipeline operation is wrapped by the recovery middleware. When
an operation fails, the recovery ladder fires immediately, in-line
with the main flow, and only surfaces to the main flow if it cannot
resolve the failure within its budget. See **Escalation Pattern**
below.

## Resumability

Every stage checkpoints its outputs to local disk and to B2. A run
interrupted mid-pipeline resumes from the last valid checkpoint; no
stage regenerates work that already succeeded. Artifact paths are
stable across resumes so the blackboard can be rehydrated
deterministically.

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

## Automated eval

The Google ADK eval harness lives at ``server/adk_eval/``. It re-exports
``pipeline_agent`` so goldens captured through ``adk web`` exercise the
same orchestrator the production server boots — no code duplication.

Run the UI locally with ``poetry run adk web .`` from ``server/``, capture
a conversation as a ``.evalset.json`` golden, drop it under
``server/adk_eval/evalsets/``, and the pytest parametrised runner
(``server/adk_eval/test_evalsets.py``) will regress it against
``server/adk_eval/test_config.json`` thresholds on every PR that touches
``server/`` (see ``.github/workflows/adk-eval.yml``). Placeholder files
tagged ``metadata.stubbed: true`` are skipped; real goldens drop that
flag to opt in.

Full usage — capturing goldens, running the harness offline, threshold
tuning — lives in ``server/adk_eval/README.md``.
