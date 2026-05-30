#!/usr/bin/env python3
"""Final audit: check which issues from the review are fixed."""

from pathlib import Path

doc = Path("/Users/orpington/Documents/economy-documentary-work/ARCHITECTURE_V7.1.md").read_text()

issues = {
    "Principles table says 11 but lists 12": "Table of 12 hard principles" in doc,
    "Parser phantom effect types removed": "_GenerateNarrationAudioEffect" not in doc,
    "Parser discriminant unified to kind": 'Field(discriminator="kind")' in doc,
    "HumanInstruction.agent → target_agent": "target_agent: str" in doc,
    "Slot addressing short form": "A1:{block.scene_num}" in doc,
    "EventStore._seen rebuilt on restart": "_rebuild_seen" in doc,
    "derive_situations checks scripted not dirty": 'if slot.status == "scripted"' in doc,
    "DurationAdjusted has slot_id": "slot_id: str = Field" in doc and "class DurationAdjusted" in doc,
    "Config has agent_models": "agent_models: dict[str, str]" in doc,
    "Config has log_dir": "log_dir: str" in doc,
    "Config has max_queue_wait_sec": "max_queue_wait_sec" in doc,
    "Config max_attempts naming unified": "max_attempts_per_block" in doc and "config.max_attempts" not in doc,
    "ScriptProposed removed": "ScriptProposed" not in doc,
    "FinalComposition removed": "FinalComposition" not in doc,
    "reconciliation_partial removed": "reconciliation_partial" not in doc,
    "HumanInstruction action human_abort fixed": "human_abort" not in doc,
    "_UpdateScriptEffect aligned with event schema": "blocks: list[ScriptBlock]" in doc and "class _UpdateScriptEffect" in doc,
    "Projection.tick() uses JSONL API": "store.read_since" in doc and 'event["sequence"]' not in doc,
    "build_memory accepts store": "store: EventStore" in doc and "def build_memory" in doc,
    "read_agent_events uses JSONL": "store.replay" in doc and "def read_agent_events" in doc,
    "Maintainer phantom effects removed": "BlockRequeued" not in doc and "OTIOUpdated" not in doc,
    "VM Worker QC uses deterministic checks": "Deterministic output validation" in doc and "deepseek-v4-flash" not in doc.split("### 11.3")[1] if "### 11.3" in doc else True,
    "VMIsolationConfig timer removed": "destroy_after_stage_seconds" not in doc,
    "_emit_budget_effect defined": "def _emit_budget_effect" in doc,
    "create_sliding_window_processor imported": "create_sliding_window_processor" in doc,
    "PipelineDeps defined": "class PipelineDeps" in doc,
    "llm_complete defined": "def llm_complete" in doc,
    "PeriodicReminderConfig defined": "class PeriodicReminderConfig" in doc,
    "SubAgentConfig defined": "class SubAgentConfig" in doc,
    "parse_agent_text_multi defined": "def parse_agent_text_multi" in doc,
    "build_clarification_request uses kind": 'effects[0].kind == "noop"' in doc,
    "EventStoreBackend Protocol sync": "def append(self" in doc.split("class EventStoreBackend")[1] if "class EventStoreBackend" in doc else False,
    "_parse_payload justified": "not needed for normal projection replay" in doc,
    "MergeIntoOTIO start_time removed": "# start_time: float" in doc or "start_time" not in doc.split("class MergeIntoOTIO")[1] if "class MergeIntoOTIO" in doc else False,
    "Handler helpers defined": "def hash_otio" in doc and "def rebuild_projections" in doc and "def notify_downstream" in doc,
    "run_agent_turn has agent_role": "agent_role: str" in doc and "def run_agent_turn" in doc,
    "Authoring workflow instructions fixed": "does not accept `instructions` param" in doc,
    "B2 credentials in Config": "b2_application_key" in doc,
    "Dependencies updated": "pydantic-ai-provenance" in doc,
    "structured_extract.py in file structure": "structured_extract.py" in doc,
    "CostTracking P2 orphan removed": "CostTracking (P2)" not in doc,
    "ProductionFailed routing mapped": "Mapping routing actions to SuggestedFix.fix_type" in doc,
}

fixed = sum(1 for v in issues.values() if v)
total = len(issues)
print(f"Fixed: {fixed}/{total}")
print()
for name, ok in issues.items():
    status = "✅" if ok else "❌"
    print(f"{status} {name}")
