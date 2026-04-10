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
