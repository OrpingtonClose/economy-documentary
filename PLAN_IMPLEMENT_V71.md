> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Plan: Implement V7.1 Architecture from Current Codebase

## Principle

Build component-by-component, test-driven, with architecture validation at phase boundaries. No code without passing tests. No integration until components work in isolation.

## Phase 0: Surgical Extraction (1 hour)

**What:** Map obsidian vault requirements against existing code. Salvage what matches. Delete what doesn't.

**Salvageable:**
- `server/effects.py` — effect type definitions (need audit against §3)
- `server/event_store.py` — SQLite append logic (need simplification)
- `server/effect_parser.py` — instructor parser (need hardening)
- `server/models/*.py` — scene, job, vm_state models (need audit)

**Delete:**
- `server/v5/` — legacy architecture
- `server/callbacks/` — hooks violate "no state machine, no rules engine"
- `server/fleet/` — fleet coordinator, scaler, router are over-engineering
- `server/dashboard/` — dashboard is not in V7.1
- `server/previews/` — previews are not in V7.1
- `server/tracing/` — tracing infrastructure is not in V7.1
- `server/plugins/` — plugin system is not in V7.1
- `server/agents/` — old agent code
- `server/strands_agents/` — Strands migration code
- `server/orchestrator/` — old orchestrator
- `server/recovery_agents.py` — recovery is agentic, not a separate module
- `server/debug_gym_*.py` — debug-gym is external tooling, not pipeline code
- `server/infra_agent.py` — infra agent is not in V7.1
- `server/maintainer.py` — maintainer is not in V7.1
- `server/gatekeeper.py` — gatekeeper is not in V7.1
- `server/vm_registry*.py` — VM registry is not in V7.1
- `server/job_queue.py` — job queue is a projection, not a module
- `server/worker_queue_adapter.py` — adapter is not in V7.1
- `server/qa_gates*.py` — QA is agentic, not a deterministic module
- `server/unit_state_machines.py` — state machines are explicitly forbidden

**Validation:** Architecture subagent reads §15 (File Structure) and validates deleted/salvaged list.

## Phase 1: Event Store + GSA Foundation (2 hours)

**Build:**
1. SQLite event store: append-only, per-run `events_{run_id}.db`
2. GSA HTTP service: `GET /` only, returns all 5 projections as JSON
3. GSA polls DB file changes, rebuilds projections from sequence 0 on restart

**Tests:**
- Append effect → read back
- GSA GET / returns valid JSON
- GSA rebuilds projections after restart (replay from 0)
- No other component reads SQLite directly

**Validation:** Subagent reads §5 (Event Store) + §2.4 (GSA) + §2.4.5 (Invariants 1-6) and validates implementation.

## Phase 2: Effect Types + Parser (2 hours)

**Build:**
1. 32 effect types per §3 as Pydantic models with `kind`, `run_id`, `effect_id` (UUIDv7), `agent`, `timestamp`
2. Effect parser: instructor + deepseek-v4-flash extracts effects from agent natural language text
3. Category-conditioned parser per §9.5

**Tests:**
- Each effect type validates correctly
- Parser extracts effects from sample agent text
- Invalid payloads are rejected
- Parser handles `NoOp`, `ClarificationRequest`, `AgentLoopDetected`

**Validation:** Subagent reads §3 (Effect Type Family) + §9.5 (Effect Parser) and validates.

## Phase 3: 5 Projections (2 hours)

**Build:**
1. OTIOProjection — slot state, dirty/clean blocks, scene ordering
2. JobProjection — pending/started/completed/failed/requeued jobs
3. VMProjection — active/history VMs, cost tracking
4. StateProjection — phase transitions, failure counts, last agent action
5. BudgetProjection — limit, spent, remaining, per-agent costs

All as pure fold functions: `projection.apply(effect) → projection`

**Tests:**
- Each projection handles all relevant effect types
- Cross-projection consistency (CompositeProjection)
- JSON serialization (sets → lists)
- Rebuild from event 0 matches incremental state

**Validation:** Subagent reads §6 (Projections) and validates.

## Phase 4: Agent HTTP Scaffold (1 hour)

**Build:**
1. Base HTTP agent class: `GET /` (health), `POST /` (wake/instruction)
2. Handler: receives POST, queries GSA via `curl`, runs LLM, parses effects, appends to store
3. No tool registration — bash_command is the ONLY tool, called via `subprocess` in agent reasoning

**Tests:**
- Agent responds to GET / with health JSON
- Agent responds to POST / with effects extracted
- Handler appends effects to event store
- Agent queries GSA before reasoning

**Validation:** Subagent reads §7 (Agent Environment) + §8 (Agent Architecture) and validates.

## Phase 5: Agents One-by-One (4 hours)

**Order:** Scenario → Audio → Video → Assembly → Provisioner

**Per agent:**
1. Write system prompt per §9 (per-agent implementations)
2. Implement `POST /` handler with GSA query + LLM call + parser + store append
3. Test with mock GSA returning synthetic state
4. Validate prompt matches vault §9

**Scenario Agent:**
- Produces `UpdateScript`, `DeleteScene`, `ReorderScenes`
- Receives back-edge failures (`gap_unexpected`, `voice_mismatch`)

**Audio Agent:**
- Produces `QueueJob` (tts), `JobApproved`, `DurationAdjusted`, `ReconciliationComplete`
- Receives `JobCompleted` / `JobFailed` from Provisioner

**Video Agent:**
- Produces `QueueJob` (ltx), `JobApproved`, `MergeIntoOTIO`
- Receives `JobCompleted` / `JobFailed` from Provisioner

**Assembly Agent:**
- Produces `PipelineComplete`, `ProductionFailed`
- Calls ffmpeg via bash_command

**Provisioner Agent:**
- Produces `VMAllocated`, `VMDeallocated`, `JobCompleted`, `JobFailed`
- Uses bash_command with vastai CLI
- Lazy provisioning: starts 1 VM, verifies health, then queues next
- Max 3 VMs total

**Tests per agent:**
- Agent produces correct effects for given GSA state
- Agent handles back-edges / failures
- Agent never produces forbidden effects

**Validation:** Subagent reads §9 + §10 (Provisioner) + §11 (VM Worker) per agent and validates.

## Phase 6: Integration + Emergent Wakes (2 hours)

**Build:**
1. Wire agents to GSA (all GET / from port 8000)
2. Orchestrator loop or emergent wake mechanism
3. No explicit state machine — phases emerge from projection state

**Tests:**
- Full pipeline with behavioral agents (deterministic, no LLM)
- 1 scene, 30-second target
- All effects emitted in correct sequence
- Final MP4 produced and validated by ffprobe

**Validation:** Subagent reads §2.3 (Emergent Pipeline Phases) + §12 (Data Flows) and validates.

## Phase 7: Real LLM Run (1 hour)

**Run:**
- DeepSeek v4 flash for all agents
- 1-scene test: "The Lacanian drive" ~30 seconds
- Debug-gym observes but does NOT intervene
- All tracing to disk

---

## Architecture Validation Strategy

Instead of per-document subagents checking every edit:

**One validation subagent per PHASE boundary.**
- Input: relevant vault sections + code written + test results
- Task: "Does this component match §X of the architecture? List deviations."
- Runs after each phase completes, before next phase starts
- Reports: PASS / FAIL with deviation list

**One test coverage subagent after Phase 5.**
- Input: all agent code + test files
- Task: "Is every agent tested? Is every effect type tested? Are there gaps?"

**Runtime validation:**
- `/cheat` skill checks: no timeouts, no env vars, no mocks outside mock_units/
- Architecture guard plugin enforces rules on every file write

---

## Deleted Code Archive

Before deleting, archive to `archive/dead_code_2026-05-30/` with README listing what was deleted and why (per §19 Discarded Propositions).

---

## Estimated Timeline

| Phase | Time | Validation |
|-------|------|------------|
| 0 | 1h | Architecture subagent |
| 1 | 2h | Architecture subagent |
| 2 | 2h | Architecture subagent |
| 3 | 2h | Architecture subagent |
| 4 | 1h | Architecture subagent |
| 5 | 4h | 5 × architecture subagents (one per agent) |
| 6 | 2h | Architecture subagent |
| 7 | 1h | Human observation |
| **Total** | **15h** | **9 validation checks** |

---

## Risk Mitigation

- If a component doesn't pass tests, STOP. Do not proceed to next phase.
- If validation subagent finds deviations, FIX before proceeding.
- If test coverage subagent finds gaps, ADD tests before integration.
- No code edits during validation — validation is read-only assessment.
