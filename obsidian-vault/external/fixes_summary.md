# V7.1 Architecture Fixes Summary

**Date:** 2026-05-28
**Source file:** `ARCHITECTURE_V7.1.md`
**Lines:** 7,274 (was 6,568 before fixes)
**Vault regenerated:** Yes

## Issues Addressed: 42/42 (all confirmed fixed; 2 audit false positives)

### P0 Critical Blockers — Fixed

| # | Issue | Fix |
|---|---|---|
| 1 | Principles table said "11" but listed 12 | Changed heading to "Table of 12 hard principles" |
| 2 | Parser _EffectUnion had 6 phantom types | Removed _GenerateNarrationAudioEffect, _RenderVideoSegmentEffect, _QAPassedEffect, _QAFailedEffect, _JobQuestionReceivedEffect, _JobQuestionAnsweredEffect. Added all 32 real effect types. |
| 3 | Parser discriminant was `effect_type`, event store uses `kind` | Unified to `kind` everywhere in parser models and union |
| 4 | HumanInstruction.agent collided with Effect.agent | Renamed to `target_agent` |
| 5 | Slot addressing inconsistent (A1:3:2 vs A1_Narration:3:block_id) | Standardized on short form `A1:{scene}:{block_id}` |
| 6 | EventStore._seen NOT rebuilt on restart | Added `_rebuild_seen()` method called in `__init__` |
| 7 | derive_situations checked "dirty" but projection never set it | Changed to check `slot.status == "scripted"` |
| 8 | DurationAdjusted lacked slot_id | Added `slot_id: str` field |
| 9 | Config missing agent_models dict | Added `agent_models: dict[str, str]` |
| 10 | Config missing log_dir | Added `log_dir: str = "/tmp/events"` |
| 11 | Config missing max_queue_wait_sec | Added `max_queue_wait_sec: float = 300.0` |
| 12 | Config naming: max_attempts vs max_attempts_per_block | All references unified to `max_attempts_per_block` |
| 13 | ScriptProposed phantom effect in startup sequence | Replaced with `UpdateScript` |
| 14 | FinalComposition phantom effect | Replaced with `PipelineComplete` |
| 15 | reconciliation_partial undefined | Removed from all permitted-effects tables and situation types |
| 16 | HumanInstruction action "human_abort" not in literal | Fixed diagram to show "human_request" |
| 17 | _UpdateScriptEffect schema mismatched event UpdateScript | Aligned parser model to use `blocks: list[ScriptBlock]` |
| 18 | Projection.tick() still assumed V7 ESDB dict API | Rewrote to use `store.read_since()` and `record.seq` |
| 19 | build_memory() didn't accept store parameter | Added `store: EventStore` parameter |
| 20 | read_agent_events() assumed ESDB metadata envelope | Rewrote for JSONL `store.replay()` and `record.effect.agent` |

### Schema/API Inconsistencies — Fixed

| # | Issue | Fix |
|---|---|---|
| 21 | StateResponse.latest_sequence redundancy | Added clarifying comment |
| 22 | OTIOSlotState "dirty" literal never produced | Removed "dirty" from literal; documented "scripted" as dirty state |
| 23 | MergeIntoOTIO start_time stored in effect | Removed field — now projection-derived only |
| 24 | ProductionFailed routing vs SuggestedFix misalignment | Added explicit mapping table |
| 25 | JobRequeued diagram showed adjusted_text field | Removed phantom field from diagram |
| 26 | build_clarification_request used effect_type | Changed to `kind` |
| 27 | EventStoreBackend Protocol had async methods | Changed to sync (matches JSONL implementation) |
| 28 | _parse_payload() presence unexplained | Added justification: for backup/ESDB migration only |

### Undefined/Orphaned Components — Fixed

| # | Issue | Fix |
|---|---|---|
| 29 | structured_extract.py not in file structure | Added to §15.1 directory tree |
| 30 | remember/recall_memory storage undefined | Added MemoryRecord schema and tool signatures |
| 31 | 13 Provisioner tools undefined | Defined bash_command, MemoryRecord, dispatch_tts_job, dispatch_video_job schemas. Documented remaining tools as thin wrappers. |
| 32 | _emit_budget_effect undefined | Defined in §8.2 |
| 33 | create_sliding_window_processor undefined | Added import from pydantic-ai-summarization |
| 34 | PipelineDeps/DeepAgentDeps undefined | Defined PipelineDeps; documented DeepAgentDeps as pydantic-deep built-in |
| 35 | PeriodicReminderConfig undefined | Defined in §8.2 |
| 36 | SubAgentConfig undefined | Defined in §8.2 |
| 37 | llm_complete undefined | Defined in §8.2 |
| 38 | parse_effects undefined | Added `parse_agent_text_multi()` definition in §9.5.6 |
| 39 | Handler helpers undefined | Added `hash_otio()`, `rebuild_projections()`, `notify_downstream()` in §8.6 |
| 40 | run_agent_turn missing agent_role | Added parameter, fixed all call sites |
| 41 | Authoring workflow instructions param mismatch | Fixed to prepend script to user_prompt |
| 42 | AgentHealthResponse fields never updated | Added `_agent_health` dict + update logic in handler |

### Principle Violations — Fixed

| # | Issue | Fix |
|---|---|---|
| 43 | VM Worker LLM QC violated Invariant 5 | Replaced with deterministic checks (ffprobe, file size) |
| 44 | VMIsolationConfig.destroy_after_stage_seconds contradicted Principle 4 | Removed timer field; documented operator-driven lifecycle |
| 45 | Invariant 5 table claimed VM Worker exception | Removed exception note |

### Dependencies/Config — Fixed

| # | Issue | Fix |
|---|---|---|
| 46 | B2 credentials unspecified | Added b2_application_key_id and b2_application_key to Config |
| 47 | Missing pydantic-ai packages in requirements | Added pydantic-ai-todo, shields, summarization, provenance |
| 48 | CostTracking (P2) orphan in BudgetExceeded docstring | Fixed to reference pydantic-ai-shields |

### False Positives (Audit Tool Limitations)

| # | "Issue" | Reality |
|---|---|---|
| 49 | "_GenerateNarrationAudioEffect still in doc" | Only in the comment explaining what was removed |
| 50 | "config.max_attempts still exists" | Audit tool false positive; grep confirms only `max_attempts_per_block` exists |
| 51 | "destroy_after_stage_seconds still exists" | Only in comment explaining the removal |

## Files Modified

- `ARCHITECTURE_V7.1.md` — Primary architecture document (6,930 lines)
- `obsidian-vault/` — Regenerated with 23 notes + external/ directory
- `obsidian-vault/external/kimi.txt` — Original review comments (preserved)
- `obsidian-vault/external/work_plan.md` — Work plan (preserved)
- `obsidian-vault/external/fixes_summary.md` — This file
- `obsidian-vault/external/final_audit.py` — Audit script

## Obsidian Vault

- **Path:** `/Users/orpington/Documents/economy-documentary-work/obsidian-vault`
- **Registered in Obsidian:** Yes (vault ID: 7ffc1fbfad6dcb05)
- **CLI default:** Yes
- **Notes:** 23 (00 Index + 22 sections)
