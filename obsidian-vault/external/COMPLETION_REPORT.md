# V7.1 Architecture Fix Completion Report

**Date:** 2026-05-28
**Status:** COMPLETE

## Document Metrics

| Metric | Before | After |
|---|---|---|
| Lines | 6,568 | 7,265 |
| Sections | 20 | 22 (+ Appendix A) |
| Effect types defined | 32 | 32 (all verified) |
| Parser models defined | 6 phantom | 32 real |
| Undefined components | 25+ | 0 |

## External Review Issues: All Addressed

### Critical Blockers (18 fixed)
- Parser phantom types removed, discriminant unified to `kind`
- HumanInstruction.agent → target_agent
- Slot addressing standardized on short form
- EventStore._seen rebuilt on restart
- derive_situations uses "scripted" not "dirty"
- DurationAdjusted gains slot_id
- Config fields added: agent_models, log_dir, max_queue_wait_sec
- Config naming unified to max_attempts_per_block
- Phantom effects removed: ScriptProposed, FinalComposition, reconciliation_partial
- _UpdateScriptEffect aligned with event schema
- Projection.tick() rewritten for JSONL API
- build_memory() and read_agent_events() accept store parameter

### Schema/API Inconsistencies (10 fixed)
- StateResponse/latest_sequence redundancy documented
- OTIOSlotState "dirty" literal removed
- MergeIntoOTIO start_time removed (projection-derived)
- ProductionFailed routing mapped to SuggestedFix.fix_type
- JobRequeued diagram cleaned
- build_clarification_request uses `kind`
- EventStoreBackend Protocol sync
- _parse_payload() justified

### Undefined Components (17 fixed)
- parse_agent_text_multi() defined with full instructor integration
- hash_otio(), rebuild_projections(), notify_downstream() defined
- _render_messages() defined
- SITUATION_TEMPLATES, ROLE_INSTRUCTIONS defined
- KIND_TO_MODEL routing table defined
- PipelineDeps, PeriodicReminderConfig, SubAgentConfig defined
- llm_complete(), _emit_budget_effect() defined
- RunRequest model defined
- MemoryRecord schema and tool signatures defined
- AgentHealthResponse update logic in handler
- structured_extract.py added to file structure

### Principle Violations (3 fixed)
- VM Worker QC replaced with deterministic checks (ffprobe)
- VMIsolationConfig timer removed
- Invariant 5 table corrected (no exceptions)

### Dependencies/Config (4 fixed)
- B2 credentials added to Config (later removed — see Post-Review Changes)
- pydantic-ai packages added to requirements
- CostTracking (P2) orphan removed
- HumanInstruction action human_abort fixed

## Post-Review Architectural Decisions (Gemini Conversation)

### 1. B2/JWT Complexity Eliminated
- No JWT, no token-vending service, no secrets on workers
- Default: artifacts stream back via HTTP in job completion response
- Optional B2: raw keys passed as env vars at VM creation time
- Config b2_application_key_id/key removed
- VMIsolationConfig jwt_ttl_seconds removed
- §11.5 rewritten as "HTTP Streaming, B2 Optional"

### 2. GSA Checkpointing Deleted
- No disk checkpoints, no /tmp/gsa-checkpoints/
- GSA is a pure stateless fold over the event log
- Replay from 0 on every restart: 500–2000 events = milliseconds
- §5.5 rewritten as "GSA Catch-Up (No Checkpointing)"
- File I/O, corruption edge cases, stale-cache risk all eliminated

### 3. Bash Resilience Documented
- §10.2.1 now explicitly documents why bash is intentional
- CLI output changes → LLM adapts (self-healing execution)
- Python code generation by agent deferred (indentation errors, import hallucinations)
- Trades deterministic parsing for operational resilience

### 4. Maintainer as External Client Only
- No internal Maintainer Agent service, no port 8006
- No service discovery needed
- Emergency intervention via curl or GUI tool posting to agent's open POST /
- §4.4 rewritten as "Maintainer Pattern (External Client Intervention)"

## Obsidian Vault

- **Path:** `/Users/orpington/Documents/economy-documentary-work/obsidian-vault`
- **Notes:** 23 (00 Index + 22 sections)
- **Registered:** Yes (vault ID: 7ffc1fbfad6dcb05)
- **CLI default:** Yes

## Verification

All `pass` statements are legitimate (projection no-ops, exception handlers, documented delegations). No `...` stubs remain. No undefined functions with 2+ references.

The architecture document is implementation-ready.
