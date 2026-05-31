> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Lessons

Durable, PR-reviewable operational knowledge from the documentary pipeline.

This tree is the **engineer-facing** memory: real numbers from real GPU VMs,
Vast.ai quirks, per-model tuning notes, cost telemetry, failure modes we hit.
It is distinct from `docs/strands-migration/AGENTS.md`, which is the
**orchestrator's** memory (re-read on every agent tick).

## Layout

- **Ledgers** — append-only tables with one row per observation. Written
  programmatically by the infra agent and other runtime components via
  `scripts/append_lesson.py`.

  - [`gpu-sizing.md`](./gpu-sizing.md) — per-VM observations
    (`vram_peak`, `disk_peak`, workload, notes).
  - [`cost-telemetry.md`](./cost-telemetry.md) — real `$/min` per
    workload × VM class.
  - [`guardian-tuning.md`](./guardian-tuning.md) — idle / max-lifetime
    values that worked (or didn't) per workload.

- **Knowledge pages** — narrative, refactorable. One file per topic. Edited
  as understanding accumulates; previous drafts live in git history.

  - [`vast-ai-quirks.md`](./vast-ai-quirks.md) — image compatibility,
    boot times, driver versions, geo latency.
  - [`model-tuning/qwen3-tts.md`](./model-tuning/qwen3-tts.md)
  - [`model-tuning/ltx-video-2.3.md`](./model-tuning/ltx-video-2.3.md)
  - [`model-tuning/whisperx.md`](./model-tuning/whisperx.md)
  - [`convergence/c01-scenario-refiner.md`](./convergence/c01-scenario-refiner.md)
  - [`convergence/evaluator-refiner-loops.md`](./convergence/evaluator-refiner-loops.md)
  - [`failure-modes/b2-checkpoint-races.md`](./failure-modes/b2-checkpoint-races.md)
  - [`failure-modes/timing-drift.md`](./failure-modes/timing-drift.md)

## Protocol

1. **Ledgers grow.** Never rewrite a row — append a new one with a later
   `observed` date. History is the point.
2. **Knowledge pages consolidate.** If a ledger shows a stable pattern
   (e.g. `qwen3-tts` peaks at 18 GB across ten runs), promote the pattern
   to the relevant knowledge page and link back to the ledger rows.
3. **Every entry carries frontmatter** so it can be parsed and filtered:

   ```yaml
   observed: 2026-04-22
   source: run_id | pr | slice | manual
   severity: info | friction | incident
   tags: [qwen3-tts, vast-ai, vram]
   ```

4. **PR-visible.** Any slice that learns something operational must
   either (a) append a ledger entry, or (b) edit a knowledge page, in
   the same PR that produced the observation.
5. **Programmatic writers** must use `scripts/append_lesson.py` rather
   than hand-writing markdown — the script validates the frontmatter
   and keeps the files parseable.

## Who writes

- **Infra agent** (`server/strands_agents/infra_agent/`) — writes
  `gpu-sizing.md`, `guardian-tuning.md`, `cost-telemetry.md` entries on
  every VM destruction with the final peak telemetry.
- **Orchestrator** — writes `convergence/*` and `failure-modes/*`
  entries when a loop fails to converge or a stage fails closed.
- **Humans** — edit knowledge pages during PR review when a ledger
  pattern becomes stable enough to summarise.
