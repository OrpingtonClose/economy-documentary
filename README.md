# Documentary Pipeline

Topic-agnostic, ADHD-friendly AI documentary generation pipeline.

Built with **Google ADK** (Agent Development Kit) + **CopilotKit AG-UI** for
real-time human-in-the-loop control.

## Architecture

```
Frontend (Next.js + CopilotKit)
    ↕ AG-UI Protocol (SSE)
Backend (FastAPI + Google ADK)
    → Scenario Director (EvaluatorOptimizer)
    → Audio Agent (Qwen3-TTS + WhisperX)
    → Visual Director (LoopAgent × 3 sub-agents)
    → Production Supervisor (LTX-2.3 on GPU)
    → Assembler Agent (ffmpeg)
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

## Quick Start

### Backend

```bash
cd server
cp .env.example .env
# Edit .env with your API keys

poetry install
poetry run uvicorn server:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

## Pipeline Phases

| Phase | Agent | What It Does |
|-------|-------|-------------|
| 1. Scenario | `scenario_director` | Generates ADHD-compliant script with V1/V2/V3 voices |
| 2. Audio | `audio_agent` | TTS generation + WhisperX word-level alignment |
| 3. Visual | `visual_director` | Content analysis → cinematic prompts → coherence check |
| 4. Production | `production_supervisor` | GPU video generation via LTX-2.3 on Vast.ai |
| 5. Assembly | `assembler_agent` | Trim, mux, concatenate into final documentary |

## ADHD Principles

- Max 45 seconds per scene
- 3 distinct voices per scene (Hook, Expert, Storyteller)
- No rhetorical questions
- Visual variety enforced by coherence evaluator
- Dopamine hooks in every scene

See [docs/ADHD_PRINCIPLES.md](docs/ADHD_PRINCIPLES.md) for details.

## Quality Gates

The frontend provides three human review points:

1. **Scenario Editor** — Edit generated scripts before audio
2. **Prompt Reviewer** — Review visual prompts and LoRA selections
3. **Clip Reviewer** — Approve/reject generated video clips

## Dashboard

Real-time pipeline monitoring via SSE:
- Phase progress tracking
- Tool call metrics
- LLM usage statistics
- HTML reports for post-mortem analysis

## Environment Variables

See [server/.env.example](server/.env.example) for the complete list.

Key variables:
- `ADK_MODEL` — Primary LLM model
- `VAST_API_KEY` — For GPU VM provisioning
- `TIMELINE_DIR` — Where OTIO timelines are stored

## Enrichment Pipeline

The `pipeline/swarm_extraction/` module provides deep claim verification:
- Multi-provider LLM fallback chain
- 40+ research tools (FRED, Perplexity, Wolfram, etc.)
- Concurrent verification subagents
- Obsidian vault generation for research results

## Project Structure

```
├── server/           # FastAPI + ADK backend
│   ├── agents/       # ADK agent definitions
│   ├── callbacks/    # ADK callbacks
│   ├── tools/        # FunctionTool wrappers
│   ├── plugins/      # ADK plugins
│   ├── dashboard/    # Real-time monitoring
│   └── server.py     # FastAPI app
├── frontend/         # Next.js + CopilotKit
│   └── src/
│       ├── app/      # Pages + API proxy
│       ├── components/ # UI components
│       └── lib/      # Types
├── pipeline/         # Core pipeline logic
│   ├── swarm_extraction/ # Research + verification
│   └── otio_timeline.py  # OTIO operations
├── scripts/          # Utility scripts
├── docs/             # Documentation
├── config.py         # Central configuration
└── test_run.py       # End-to-end test
```
