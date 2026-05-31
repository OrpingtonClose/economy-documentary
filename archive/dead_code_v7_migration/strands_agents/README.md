> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# `server/strands_agents/`

Strands + DeepAgent reimplementation of the documentary pipeline.

Canonical spec lives at
[`docs/strands-migration/`](../../docs/strands-migration/README.md).
Build order and definition-of-done per component are in
[`SEQUENCE.md`](../../docs/strands-migration/SEQUENCE.md).

## Layout

```
server/strands_agents/
├── leaves/          # Strands Agents (components 01, 03, 06, 07, 08)
├── tools/           # @tool functions (02, 04, 11, 12) + AsyncTaskPool
├── subagents/       # deepagents SubAgent TypedDicts (09, 10, 13)
├── evals/
│   ├── evaluators/  # Custom Evaluator subclasses
│   ├── experiments/ # Experiment.to_file() JSON, one per component
│   └── simulators/  # ToolSimulator configs (GPU workers, TTS)
├── memory/          # AGENTS.md seeded into MemoryMiddleware
└── tests/
    ├── unit/        # pytest unit tests, no network
    └── integration/ # pytest integration tests, API keys required
```

## Running locally

```bash
cd server
poetry install --with strands
poetry run pytest strands_agents/tests/unit/ -v
```

## CI

`.github/workflows/strands-evals.yml` runs the unit tests and each
checked-in experiment on every PR that touches `server/strands_agents/`
or the workflow file itself. The ADK CI (`adk-eval.yml`) is untouched.

## Status

Phase 0 — scaffolding only. No pipeline execution path yet.
`run_pipeline.py --pipeline=strands` raises `NotImplementedError` until
component 14 lands.
