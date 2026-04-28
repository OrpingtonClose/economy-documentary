# Documentary Pipeline

AI documentary generation pipeline. Give it a topic, get a movie.

## Make a Movie (Start Here)

```bash
./make_movie.sh "The History of Coffee"
```

That one command handles everything — dependencies, configuration, and
running the pipeline. See [QUICKSTART.md](QUICKSTART.md) for details.

**Requirements:** Python 3.12+. That's it.

## How It Works

The pipeline runs 5 agents in sequence:

1. **Scenario Director** — writes an ADHD-friendly script (multiple voices, short scenes)
2. **Audio Agent** — generates narration via TTS + word-level alignment
3. **Visual Director** — creates cinematic visual prompts for each scene
4. **Production Supervisor** — generates video clips on GPU (LTX-2.3)
5. **Assembler Agent** — combines everything into a final documentary

Built with Google ADK (Agent Development Kit). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Running Modes

| Mode | Command | What It Does | Needs GPU? |
|------|---------|-------------|------------|
| **Test** | `./make_movie.sh "Topic"` | Full pipeline with simulated media | No |
| **Quick test** | `./make_movie.sh "Topic" --quick` | 2 scenes, ~1 min | No |
| **Production** | `./make_movie.sh "Topic" --corpus research.md --production` | Real video | Yes |

## Manual Setup (Advanced)

If you prefer to run things manually instead of using `make_movie.sh`:

```bash
cd server
cp .env.example .env          # Create config (edit to add API keys)
poetry install                 # Install dependencies
poetry run python run_pipeline.py --topic "Your Topic" --corpus research.md --test-mode
```

### Frontend (Optional)

The frontend provides a dashboard with human review gates:

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
- `DOCUMENTARY_TEST_MODE` — Enable test mode (no GPU needed)
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
