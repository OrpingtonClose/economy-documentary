#!/usr/bin/env python3
"""Batch 5: Fix remaining undefined components and stubs."""

from pathlib import Path

DOC = Path("/Users/orpington/Documents/economy-documentary-work/ARCHITECTURE_V7.1.md")
text = DOC.read_text()

# 1. Add _render_messages definition
old = 'async def otio_aware_compress(ctx, messages, **kwargs):'
new = '''def _render_messages(messages: list) -> str:
    """Flatten message history to a single string for compaction LLM.

    V7.1: Defined here -- was referenced but never shown.
    """
    parts = []
    for m in messages:
        if hasattr(m, "content"):
            parts.append(str(m.content))
        else:
            parts.append(str(m))
    return "\\n\\n".join(parts)


async def otio_aware_compress(ctx, messages, **kwargs):'''
if old in text:
    text = text.replace(old, new)
    print("OK _render_messages defined")
else:
    print("MISS _render_messages insertion point")

# 2. Remove human_override from DurationAdjusted diagram
old = '''      |-- DurationAdjusted --------------------->|
      |   block=A1:3:1                           |
      |   measured=5.5                           |
      |   human_override=true                    |
      |   reason="operator                       |
      |     approved"                            |'''
new = '''      |-- DurationAdjusted --------------------->|
      |   block=A1:3:1                           |
      |   measured=5.5                           |
      |   slot_id="A1:3:1"                       |
      |   scripted_sec=5.0                       |'''
if old in text:
    text = text.replace(old, new)
    print("OK DurationAdjusted diagram fixed")
else:
    print("MISS DurationAdjusted diagram")

# 3. Define RunRequest
old = '''The Scenario Agent handler:
1. Validates the payload against `RunRequest` Pydantic model.
2. Appends `PipelineStarted` to JSONL file `events_{run_id}.jsonl`.'''
new = '''```python
class RunRequest(BaseModel):
    """V7.1: Payload for initiating a new pipeline run."""
    run_id: str = Field(default_factory=lambda: str(uuid7()))
    screenplay_text: str = Field(..., min_length=1)
    budget_usd: float = Field(default=10.0, gt=0.0)
    output_path: str = Field(default="/tmp/final_documentary.mp4")
```

The Scenario Agent handler:
1. Validates the payload against `RunRequest` Pydantic model.
2. Appends `PipelineStarted` to JSONL file `events_{run_id}.jsonl`.'''
if old in text:
    text = text.replace(old, new)
    print("OK RunRequest defined")
else:
    print("MISS RunRequest insertion point")

# 4. Add SITUATION_TEMPLATES and ROLE_INSTRUCTIONS before derive_situations
old = '''def derive_situations(
    projections: GlobalStateResponse,
    role: Literal["scenario", "audio", "video", "assembly", "provisioner"],
    config: Config,
) -> list[Situation]:'''
new = '''# V7.1 fix: Defined here -- was referenced but never shown.
SITUATION_TEMPLATES: dict[str, str] = {
    "fresh_dirty_block": (
        "=== SLOT: {slot_id} ===\\n"
        "{text_snippet}\\n"
        "TARGET: {scripted_sec}s | ATTEMPTS: {attempts}/{max_attempts}\\n"
        "WHAT IS HAPPENING: This block needs audio generation and measurement.\\n"
        "WHAT TO DO: Emit QueueJob(job_type=tts, ...) for this block."
    ),
    "measurement_complete_pass": (
        "=== SLOT: {slot_id} === MEASURED: {measured_sec}s | TARGET: {scripted_sec}s\\n"
        "Within tolerance. Emit DurationAdjusted to update OTIO."
    ),
    "measurement_complete_fail": (
        "=== SLOT: {slot_id} === MEASURED: {measured_sec}s | TARGET: {scripted_sec}s\\n"
        "Outside tolerance. Emit ReconciliationFailed or requeue."
    ),
    "block_at_max_attempts": (
        "=== SLOT: {slot_id} === ATTEMPTS EXHAUSTED: {attempts}/{max_attempts}\\n"
        "Emit ReconciliationFailed with duration_unrecoverable."
    ),
    "vm_stale": (
        "VM {vm_id} last_seen={last_seen}s ago. Health check failed.\\n"
        "Consider deallocating and re-provisioning."
    ),
    "vm_provision_failed": (
        "VM provision failed: {reason}. Budget impact: ${cost_usd:.2f}\\n"
        "Emit VMProvisionFailed and consider alternative GPU."
    ),
    "job_queued_long": (
        "Job {job_id} queued for {wait_sec}s. VM count: {vm_count}.\\n"
        "Provisioner should allocate VM or diagnose."
    ),
    "reconciliation_complete_all": (
        "All {total} blocks pass tolerance. Audio pipeline complete.\\n"
        "POST wake to Video Agent."
    ),
    "assembly_ready": (
        "All video approved. {slot_count} slots ready for assembly.\\n"
        "Emit PipelineComplete when final mux validated."
    ),
    "pipeline_budget_warning": (
        "Budget: ${spent:.2f} / ${cap:.2f} ({pct:.0%}). Approaching limit."
    ),
    "pipeline_budget_critical": (
        "Budget: ${spent:.2f} / ${cap:.2f} ({pct:.0%}). CRITICAL.\\n"
        "Emit PipelineAborted or request budget_override."
    ),
    "agent_loop_detected": (
        "LOOP DETECTED: {agent} emitted same effect {count} times.\\n"
        "Last effects: {effect_kinds}. Emit ClarificationRequest."
    ),
    "human_instruction_pending": (
        "HUMAN INSTRUCTION for {target_agent}: {instruction_text}\\n"
        "Priority: {priority}. Action: {action}."
    ),
    "noop_all_clean": (
        "No active situations. Pipeline state is clean.\\n"
        "Emit NoOp and await next wake."
    ),
}

ROLE_INSTRUCTIONS: dict[str, str] = {
    "scenario": (
        "You are the Scenario agent. You write and revise narration scripts.\\n"
        "Every block must specify speaker, duration_sec, and scene_num."
    ),
    "audio": (
        "You are the Audio agent. Own narration reconciliation:\\n"
        "(1) Queue TTS jobs for scripted blocks. (2) On JobCompleted, measure.\\n"
        "(3) Compare measured vs scripted (+-15% or +-0.25s).\\n"
        "Within -> DurationAdjusted; outside -> ReconciliationFailed -> requeue.\\n"
        "Max 5 attempts per block, $2 TTS budget."
    ),
    "video": (
        "You are the Video agent. Own video generation:\\n"
        "Queue LTX jobs for approved audio blocks. Judge quality.\\n"
        "Approve -> JobApproved; reject -> JobRequeued."
    ),
    "assembly": (
        "You are the Assembly agent. Final mux and validation.\\n"
        "Merge approved clips. Validate dual-threshold tolerance.\\n"
        "Emit PipelineComplete on success."
    ),
    "provisioner": (
        "You are the Provisioner. Most intelligence-requiring component.\\n"
        "Provision VMs, dispatch jobs, learn from failures.\\n"
        "Use bash_command for Vast.ai. Use remember/recall_memory for learning."
    ),
}


def derive_situations(
    projections: GlobalStateResponse,
    role: Literal["scenario", "audio", "video", "assembly", "provisioner"],
    config: Config,
) -> list[Situation]:'''
if old in text:
    text = text.replace(old, new)
    print("OK SITUATION_TEMPLATES, ROLE_INSTRUCTIONS defined")
else:
    print("MISS derive_situations insertion point")

# 5. Add KIND_TO_MODEL before EffectUnion
old = '''#### 3.10.1 Discriminated union definition

```python
from typing import Annotated, Union'''
new = '''#### 3.10.1 KIND_TO_MODEL routing table

```python
# V7.1 fix: Defined here -- was referenced throughout but never shown.
KIND_TO_MODEL: dict[str, type[Effect]] = {
    "update_script": UpdateScript,
    "delete_scene": DeleteScene,
    "reorder_scenes": ReorderScenes,
    "queue_job": QueueJob,
    "job_started": JobStarted,
    "job_completed": JobCompleted,
    "job_failed": JobFailed,
    "job_requeued": JobRequeued,
    "job_approved": JobApproved,
    "duration_adjusted": DurationAdjusted,
    "reconciliation_failed": ReconciliationFailed,
    "reconciliation_complete": ReconciliationComplete,
    "vm_allocated": VMAllocated,
    "vm_deallocated": VMDeallocated,
    "vm_provision_failed": VMProvisionFailed,
    "merge_into_otio": MergeIntoOTIO,
    "delete_from_otio": DeleteFromOTIO,
    "pipeline_started": PipelineStarted,
    "pipeline_complete": PipelineComplete,
    "pipeline_aborted": PipelineAborted,
    "budget_set": BudgetSet,
    "budget_exceeded": BudgetExceeded,
    "vast_global_state_observed": VASTGlobalStateObserved,
    "execute_raw_bash": ExecuteRawBash,
    "human_instruction": HumanInstruction,
    "clarification_request": ClarificationRequest,
    "agent_loop_detected": AgentLoopDetected,
    "production_failed": ProductionFailed,
    "measurement_requested": MeasurementRequested,
    "audio_measured": AudioMeasured,
    "video_measured": VideoMeasured,
    "noop": NoOp,
}
```

#### 3.10.2 Discriminated union definition

```python
from typing import Annotated, Union'''
if old in text:
    text = text.replace(old, new)
    print("OK KIND_TO_MODEL defined")
else:
    print("MISS KIND_TO_MODEL insertion point")

DOC.write_text(text)
print("Batch 5 written")
