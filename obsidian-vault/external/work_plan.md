# V7.1 Architecture Fix Work Plan

## Source of Comments
`/obsidian-vault/external/kimi.txt` contains two full assessments:
1. **V7 Assessment** — 35 issues across critical blockers, schema inconsistencies, undefined components, principle violations, operational gaps
2. **V7.1 Assessment** — 50 issues, including 11 new ones introduced by V7.1 JSONL migration

## Strategy
Fix ARCHITECTURE_V7.1.md directly. Regenerate Obsidian vault after. Use intermediary files here for reasoning.

## Phase 1: P0 Critical Blockers (Will prevent build/runtime)
| # | Issue | Fix Approach |
|---|---|---|
| 1 | Parser _EffectUnion has 6 phantom types | Remove from parser union OR add to canonical effects |
| 2 | Parser discriminant `effect_type` vs event store `kind` | Unify to `kind` everywhere |
| 3 | HumanInstruction.agent collides with Effect.agent | Rename to `target_agent` |
| 4 | Slot addressing inconsistency A1:3:2 vs A1_Narration:3:block_id | Standardize on short form everywhere |
| 5 | Per-run serialization asyncio.Lock not cross-process | Document single-process constraint explicitly |
| 6 | reconciliation_partial undefined | Remove from permitted-effects tables |
| 7 | ScriptProposed / FinalComposition phantom | Replace with real effect names |
| 8 | _UpdateScriptEffect schema mismatch | Align parser model with event schema |
| 9 | JobRequeued adjusted_text field missing | Remove from diagram, keep new_params only |
| 10 | HumanInstruction action "human_abort" not in literal | Fix to "emergency_abort" |
| 11 | Table says 11 principles but lists 12 | Fix text or remove one principle |
| 12 | structured_extract.py not in file structure | Add it |
| 13 | remember/recall_memory undefined | Add minimal schema |
| 14 | 13 Provisioner tools undefined | Define schemas or remove |
| 15 | VM Worker LLM QC violates Invariant 5 | Move QC to Audio/Video Agent |
| 16 | Provisioner tools mutate before effects | Document as acknowledged risk with idempotent design |
| 17 | B2 credentials unspecified | Add to Config |
| 18 | _emit_budget_effect undefined | Define it |
| 19 | create_sliding_window_processor undefined | Add import/definition |
| 20 | PipelineDeps/DeepAgentDeps undefined | Define them |
| 21 | Config.max_queue_wait_sec missing | Add field |
| 22 | LoopDetectorConfig orphaned | Wire it or remove |
| 23 | CostTracking (P2) orphan | Remove |

## Phase 2: V7.1 New Blockers
| # | Issue | Fix Approach |
|---|---|---|
| 24 | EventStore._seen NOT rebuilt on restart | Add rebuild in __init__ |
| 25 | derive_situations checks "dirty" but projection sets "scripted" | Change derive_situations to check "scripted" |
| 26 | DurationAdjusted lacks slot_id | Add slot_id field |
| 27 | Config missing agent_models dict | Add it |
| 28 | Config missing log_dir | Add it |
| 29 | max_attempts vs max_attempts_per_block | Unify naming |
| 30 | SITUATION_TEMPLATES {max_attempts} missing | Add to facts dict |
| 31 | Projection.tick()/build_memory() assume ESDB dict API | Rewrite for JSONL EventRecord API |
| 32 | Maintainer references BlockRequeued/OTIOUpdated | Remove phantom effects |
| 33 | Authoring workflow instructions param mismatch | Fix to use agent.run(user_prompt=...) with instructions set at construction |
| 34 | VMIsolationConfig.destroy_after_stage_seconds contradicts Principle 4 | Remove timer; use operator-driven destruction |
| 35 | Missing dependencies in requirements | Add them |
| 36 | PipelineDeps extends undefined DeepAgentDeps | Define DeepAgentDeps or remove extends |
| 37 | PeriodicReminderConfig/SubAgentConfig undefined | Define or remove |
| 38 | llm_complete undefined | Define it |
| 39 | DurationAdjusted diagram human_override phantom | Remove from diagram |
| 40 | RunRequest undefined | Define it |
| 41-50 | Various signature mismatches | Fix all handler code examples |

## Phase 3: Schema/API Inconsistencies
- StateResponse.latest_sequence redundancy
- OTIOSlotState "dirty" literal
- MergeIntoOTIO start_time
- ProductionFailed routing vs SuggestedFix
- EventStoreBackend Protocol async vs sync
- _parse_payload() justification

## Phase 4: Operational Gaps
- GSA failover
- JSONL error handling
- Agent wake delivery retry
- VM Worker callback schema
- Model download failure
- Checkpoint directory persistence
- Causation ID tracking across restarts
- AgentHealthResponse update mechanism
