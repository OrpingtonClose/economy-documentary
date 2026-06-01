---
{
  "title": "Effect Type Family \u2014 Complete Schemas",
  "section": "3",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[02 - System Topology|System Topology]] | [[00 - Index|Index]] | [[04 - Rules as Prompt No State Machine No Rules Engine Code|Rules as Prompt (No State Machine, No Rules Engine Code)]] ->

# Effect Type Family — Complete Schemas


All pipeline mutations pass through the SQLite event store as **effects** — Pydantic v2 models serialized to JSON lines. Every effect carries `effect_id` (UUIDv7 for client-side idempotency), `agent` (which component produced it), and `timestamp` (seconds since epoch). The `kind` field serves as the discriminant for parsing and union dispatch.

This section defines 32 concrete effect types organized into 8 families, plus the base `Effect` model and the `ReconciliationFailureDetail` and `SuggestedFix` sub-models. All together, 35 Pydantic models. Every model is a complete, runnable schema with type annotations, `Literal` discriminants, and `Field` constraints. The section closes with the `EffectUnion` discriminated union definition and the `KIND_TO_MODEL` routing table used by the parser.

Naming convention: **imperative** for agent requests (`QueueJob`, `MergeIntoOTIO`, `DeleteScene`), **past-tense** for system-reported outcomes (`JobCompleted`, `AudioMeasured`, `PipelineComplete`).

**V7.1 note:** `ExecuteRawBash` is removed. Agents use `bash_command` as their tool
directly; bash execution is internal to the turn, not an effect extracted from
output.

---

### 3.1 Base Effect Model

#### 3.1.1 Effect base class

```python
from __future__ import annotations

import time
from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field
from uuid_extensions import uuid7


class Effect(BaseModel):
    """Base for all effect types. NEVER instantiated directly.

    Fields present on every effect emitted into the event store:
    - effect_id:     UUIDv7 generated client-side for idempotent retries
    - kind:          Literal discriminant string (overridden per subclass)
    - agent:         component that produced the effect (e.g. "scenario")
    - timestamp:     seconds since epoch at creation time

    The SQLite event store deduplicates on effect_id via unique constraint.
    Client-side generation means an agent can retry an append with the same
    effect_id and the duplicate is silently dropped by the server.
    """
    effect_id: UUID = Field(default_factory=uuid7)
    kind: str = "effect"  # overridden per subclass via Literal
    agent: str
    timestamp: float = Field(default_factory=time.time)
```

`effect_id` uses UUIDv7 because it encodes a timestamp in the high bits, making event logs naturally time-sortable without leaking sequence gaps. Client-side generation means an agent can retry a failed append with the same `effect_id` and the SQLite store silently drops duplicates.

#### 3.1.2 Event store idempotency

The SQLite event store provides idempotency on `effect_id`. The client passes `effect_id` when appending. If the same `effect_id` is appended twice, the store treats the second append as a no-op by handling IntegrityError on insertion.

| Field | Type | Source | Purpose |
|---|---|---|---|
| `effect_id` | `UUID` | `uuid7()` client-side | idempotency key; survives retry |
| `kind` | `str` (Literal per subclass) | agent/parser | discriminant for `EffectUnion` |
| `agent` | `str` | caller | attribution for loop detection |
| `timestamp` | `float` | `time.time()` | wall-clock ordering aid |

---

### 3.2 Script Effects

Produced by the Scenario Agent (port 8001). These effects mutate the OTIO timeline's narrative track.

#### 3.2.1 UpdateScript, DeleteScene, ReorderScenes

```python
class ScriptBlock(BaseModel):
    """A single narration block within an UpdateScript."""
    scene_num: int = Field(..., ge=1, description="1-based scene index")
    block_id: str = Field(..., description="stable identifier for this narration block")
    speaker: str = Field(..., description="voice role: narrator, guest_a, etc.")
    text: str = Field(..., min_length=1, description="narration text")
    pronunciation_hints: list[str] = Field(default_factory=list)
    visual_notes: str = ""
    dopamine_hook: str = ""
    duration_sec: float = Field(..., gt=0.0, description="target duration in seconds")


class UpdateScript(Effect):
    """Write or revise one or more scene narration blocks.

    UpdateScript carries a list of ScriptBlock objects. The Timeline
    performs an upsert/deep merge: blocks whose (text, speaker, duration_sec)
    are unchanged preserve their measured_sec and status. Only changed or
    new blocks are marked status='scripted' (the 'dirty' state for reconciliation). Blocks whose scene_num
    is absent from the list are removed.
    """
    kind: Literal["update_script"] = "update_script"
    blocks: list[ScriptBlock] = Field(..., min_length=1)


class DeleteScene(Effect):
    """Remove a scene and all its narration blocks from the timeline."""
    kind: Literal["delete_scene"] = "delete_scene"
    scene_num: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1)


class ReorderScenes(Effect):
    """Change scene order. new_order[i] is the scene_num that should occupy position i+1.

    The Timeline resequences the top-level timeline tracks so that
    scene N moves to the position specified. All narration and video slots
    attached to a scene move with it.
    """
    kind: Literal["reorder_scenes"] = "reorder_scenes"
    new_order: list[int] = Field(..., min_length=1)
```

---

### 3.3 Job Effects

Produced by the Audio Agent (port 8002), Video Agent (port 8003), and the Provisioner agent (port 8081). These effects manage the lifecycle of media-generation work units.

#### 3.3.1 QueueJob, JobStarted, JobCompleted, JobFailed, JobRequeued, JobApproved

```python
class QueueJob(Effect):
    """Demand creation of a media artifact by a VM worker.

    The Provisioner reads QueueJob from the Jobs and matches it to
    a Vast.ai offer (job_type="tts" -> Qwen3-TTS GPU; job_type="ltx" -> LTX GPU).
    Once a VM is allocated, the job is considered "pending".
    """
    kind: Literal["queue_job"] = "queue_job"
    job_id: str = Field(..., description="stable unique job identifier")
    job_type: Literal["tts", "ltx"]
    scene_num: int = Field(..., ge=1)
    block_id: str
    slot_id: str = Field(..., description="OTIO slot where the result belongs")
    params: dict = Field(default_factory=dict, description="type-specific generation params")


class JobStarted(Effect):
    """VM worker accepted the job (returned 202 Accepted). Job is now running."""
    kind: Literal["job_started"] = "job_started"
    job_id: str
    vm_instance_id: str
    started_at: float = Field(default_factory=time.time)


class JobCompleted(Effect):
    """VM worker finished successfully; artifact is ready for quality review."""
    kind: Literal["job_completed"] = "job_completed"
    job_id: str
    artifact_uri: str = Field(..., description="URI to the generated artifact. Default: HTTP URL from VM worker. Optional: B2 URI if external storage configured.")
    duration_sec: float = Field(..., ge=0.0, description="actual media duration")
    vm_instance_id: str
    measurements: list[float] = Field(
        default_factory=list,
        description="WhisperX measurements from VM worker (3 runs)",
    )


class JobFailed(Effect):
    """VM worker failed. failure_category drives retry vs escalation policy."""
    kind: Literal["job_failed"] = "job_failed"
    job_id: str
    error_message: str
    failure_category: Literal[
        "oom",           # GPU out of memory
        "bad_prompt",    # malformed generation params
        "model_load_error",  # model weights failed to load
        "disk_full",     # VM out of disk
        "network",       # network error during model download or upload
        "cuda_error",    # CUDA runtime failure
        "unknown",       # uncategorized failure
    ]
    vm_instance_id: str
    retryable: bool = True
    retry_count: int = Field(default=0, ge=0, description="how many times this job has been retried")


class JobRequeued(Effect):
    """Artistry rejection: previous output did not meet quality bar.

    The parser extracts this from Audio or Video Agent output when a JobCompleted artifact fails
    quality review. new_params carries adjusted generation parameters for
    the retry attempt (e.g., different voice speed, revised prompt).
    """
    kind: Literal["job_requeued"] = "job_requeued"
    job_id: str
    reason: str = Field(..., min_length=1, description="why the previous attempt was rejected")
    new_params: dict | None = None


class JobApproved(Effect):
    """Artistry approval: artifact passes quality review, ready for OTIO merge."""
    kind: Literal["job_approved"] = "job_approved"
    job_id: str
    artifact_uri: str
    quality_notes: str = ""
    reviewed_by: str = Field(default="agent", description="'agent' or human name")
```

#### 3.3.2 JobFailed.failure_category routing

| Category | Meaning | Default Action | Retryable |
|---|---|---|---|
| `oom` | GPU out of memory | requeue with lower batch size | yes |
| `timeout` | VM-side timeout (legacy) | removed in V7; operator intervenes | yes |
| `bad_prompt` | Malformed params | fix params, requeue | yes |
| `model_load_error` | Weights load failure | requeue on fresh VM | yes |
| `disk_full` | VM disk exhausted | deallocate VM, requeue | yes |
| `network` | Transient network error | retry with backoff | yes |
| `cuda_error` | CUDA runtime failure | requeue on different GPU | yes |
| `unknown` | Uncategorized | emit `ClarificationRequest` | no |

The `retryable` field is a hint. The Provisioner agent may override it based on retry count (e.g., force `retryable=False` after 3 consecutive failures of the same job).


---

### 3.4 Reconciliation Effects

Produced by the Audio Agent and the Provisioner agent during audio reconciliation. These effects implement the tight TTS-measure-adjust loop.

#### 3.4.1 AudioGenerated, AudioMeasured, DurationAdjusted

```python
class AudioGenerated(Effect):
    """TTS WAV produced by a VM worker. Artifact awaits WhisperX measurement."""
    kind: Literal["audio_generated"] = "audio_generated"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    artifact_uri: str


class AudioMeasured(Effect):
    """WhisperX measured the actual spoken duration of a generated WAV.

    Three independent WhisperX runs produce three measurements (decision C2).
    The median of `measurements` is the authoritative measured duration used
    for tolerance checking. All three values are stored for debugging variance.
    """
    kind: Literal["audio_measured"] = "audio_measured"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    measured_sec: float = Field(..., description="median of measurements (authoritative)")
    measurements: list[float] = Field(
        default_factory=list,
        description="all three WhisperX measurements, unsorted",
    )
    whisperx_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DurationAdjusted(Effect):
    """Measured duration within tolerance; OTIO slot updated with authoritative value.

    The Audio Agent computes delta = measured_sec - scripted_sec. If
    |delta| <= max(15% * scripted_sec, 0.25s) the block passes. The OTIO
    Projection updates the slot's source_range to match measured_sec.

    **V7.1 fix:** Added `slot_id` so Timeline can resolve the clip
    unambiguously. `block_id` alone does not include the track prefix.

    Note: delta_sec and tolerance_sec are computed by projections, not stored
    in the effect. This prevents stale derived values if the tolerance formula
    changes.
    """
    kind: Literal["duration_adjusted"] = "duration_adjusted"
    block_id: str
    slot_id: str = Field(..., description="Canonical slot address, e.g. 'A1:3:2'")
    scene_num: int
    voice_role: str
    scripted_sec: float
    measured_sec: float
```

#### 3.4.2 ReconciliationFailed, ReconciliationComplete

```python
class ReconciliationFailed(Effect):
    """One or more blocks failed the tolerance check. Retry or escalate.

    The failure_type field determines routing:
    - duration_mismatch -> requeue with adjusted TTS params (normal retry)
    - duration_unrecoverable -> per-block attempt limit exceeded
      This triggers a back-edge to SCRIPT because the target duration is
      physically impossible for the given text.
    """
    kind: Literal["reconciliation_failed"] = "reconciliation_failed"
    agent: str
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    failures: list[ReconciliationFailureDetail] = Field(default_factory=list)
    worst_delta_sec: float
    suggested_adjustments: list[dict] = Field(default_factory=list)
    failure_type: Literal["duration_mismatch", "duration_unrecoverable"] = "duration_mismatch"


class ReconciliationComplete(Effect):
    """All narration blocks pass tolerance. OTIO is now authoritative.

    This effect is the gateway from AUDIO_RECONCILE to VIDEO_PRODUCTION.
    Agents check for ReconciliationComplete and clean blocks to decide
    whether to begin video generation.
    """
    kind: Literal["reconciliation_complete"] = "reconciliation_complete"
    agent: str
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    worst_delta_sec: float
    total_measured_sec: float
```

#### 3.4.3 ReconciliationFailureDetail sub-model

```python
class ReconciliationFailureDetail(BaseModel):
    """Per-block failure diagnostic embedded in ReconciliationFailed.

    Not a top-level effect — has no `kind` field and is NOT in EffectUnion.
    """
    block_id: str
    scene_num: int
    phrase_idx: int = Field(..., description="index of phrase within block")
    voice: str
    scripted_sec: float
    measured_sec: float
    delta_sec: float
    ratio: float = Field(..., description="measured / scripted")
    message: str = Field(..., description="human-readable diagnostic")
    attempt_number: int = Field(default=1, ge=1, description="which reconciliation attempt this was")
```

| Reconciliation Effect | Producer | Next Action |
|---|---|---|
| `AudioGenerated` | Provisioner / VM Worker | Run WhisperX (3x), emit `AudioMeasured` |
| `AudioMeasured` | Audio Agent | Parser extracts `DurationAdjusted` or `ReconciliationFailed` from Audio Agent output after tolerance computation |
| `DurationAdjusted` | Audio Agent | Timeline updates slot; block passes |
| `ReconciliationFailed` | Audio Agent | Requeue with adjusted params, or escalate if `duration_unrecoverable` |

| `ReconciliationComplete` | Audio Agent | Video Agent may begin VIDEO_PRODUCTION when all blocks clean |

---

### 3.5 VM Effects

Produced by the Provisioner agent (port 8081). These effects track the lifecycle of ephemeral GPU instances rented from Vast.ai.

#### 3.5.1 VMAllocated, VMDeallocated, VMProvisionFailed, VMObserved

```python
class VMAllocated(Effect):
    """GPU instance created and ready for job assignment.

    The parser extracts this from the Provisioner's text after successfully creating a Vast.ai instance
    and verifying that the worker HTTP endpoint (port 9000+) responds to GET /.
    """
    kind: Literal["vm_allocated"] = "vm_allocated"
    instance_id: str = Field(..., description="Vast.ai instance ID")
    role: Literal["tts", "ltx"] = Field(..., description="worker role determines GPU type")
    offer_id: str = Field(..., description="Vast.ai offer ID that was accepted")
    worker_url: str = Field(..., description="full URL including port, e.g. http://1.2.3.4:9000")
    gpu_type: str = Field(..., description="GPU model, e.g. 'RTX 4090'")
    cost_per_hour: float = Field(..., gt=0.0)


class VMDeallocated(Effect):
    """GPU instance destroyed. Final cost is recorded for budget tracking."""
    kind: Literal["vm_deallocated"] = "vm_deallocated"
    instance_id: str
    reason: Literal[
        "job_done",       # worker finished all assigned jobs
        "cost_limit",     # exceeded per-VM cost threshold
        "stale",          # operator manually deallocated stuck VM
        "provision_failed",  # never reached healthy state
        "manual",         # human operator destroyed via instruction
    ]
    final_cost: float = Field(default=0.0, ge=0.0)
    runtime_sec: float = Field(default=0.0, ge=0.0)


class VMProvisionFailed(Effect):
    """Provisioner could not create a VM for a pending job.

    On repeated failures (configurable threshold, default 3), the Provisioner
    halts and produces text from which the parser extracts `ClarificationRequest` for human intervention. It does not
    attempt creative recovery — agent reasoning about the failure.
    """
    kind: Literal["vm_provision_failed"] = "vm_provision_failed"
    offer_id: str = ""
    job_id: str = ""
    error_message: str
    failure_category: Literal[
        "no_offers",      # no Vast.ai offers match requirements
        "offer_taken",    # offer was rented by another user
        "payment_failed", # billing issue
        "boot_timeout",   # instance created but never became healthy
        "ssh_failed",     # cannot reach worker endpoint
        "unknown",
    ]
    retryable: bool = True
    consecutive_failures: int = Field(default=1, ge=1)


class VMObserved(Effect):
    """Provisioner detected drift between event-derived VM state and Vast.ai reality.

    The Provisioner polls Vast.ai API and compares reported
    instance status against the VMs' internal model. When drift is
    detected, the parser extracts VMObserved from its text so the projection can reconcile.

    The Provisioner never auto-corrects. All drift surfaces to the operator
    via ClarificationRequest. Principle 9: no automatic stale-state detection.
    """
    kind: Literal["vm_observed"] = "vm_observed"
    instance_id: str
    observed_status: Literal[
        "running", "offline", "not_found", "unknown"
    ] = Field(..., description="what Vast.ai API reports")
    expected_status: Literal[
        "running", "offline", "not_found", "unknown"
    ] = Field(..., description="what VMs believe")
    drift_description: str = Field(..., description="human-readable drift summary")
    corrective_action: Literal[
        "none",           # minor drift, logged only
        "escalate",       # unresolvable, parser extracts ClarificationRequest
    ] = "none"
```


---

### 3.6 OTIO Effects

Produced by the Audio Agent and Video Agent after artistry approval. These effects merge approved media artifacts into the OTIO timeline.

#### 3.6.1 MergeIntoOTIO, DeleteFromOTIO

```python
class MergeIntoOTIO(Effect):
    """Approved clip enters the OTIO timeline at the specified track and slot.

    The Timeline finds the existing `otio.schema.Clip` by `slot_id`
    (see §6.1.3 for slot addressing) and replaces its `MissingReference`
    with an `ExternalReference` pointing to `artifact_uri`. The clip's
    `source_range` is updated to `duration_sec`. The `documentary` metadata
    is updated with `status="delivered"` and `artifact_uri`.
    """
    kind: Literal["merge_into_otio"] = "merge_into_otio"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str = Field(..., description="Canonical slot address, e.g. 'A1:3:2' (§6.1.3)")
    artifact_uri: str = Field(..., description="URI to the approved media file (HTTP or B2)")
    track_name: Literal["A1_Narration", "V1_Video"] = Field(..., description="Target track (§6.1.3)")
    # V7.1 fix: start_time is computed by Timeline from preceding clips,
    # not stored in the effect. Storing it invites stale-data bugs.
    # start_time: float  # REMOVED — projection-derived
    duration_sec: float = Field(..., gt=0.0)
    transition_type: Literal["cut", "dissolve", "none"] = "cut"
    transition_duration_sec: float = Field(default=0.0, ge=0.0)


class DeleteFromOTIO(Effect):
    """Remove a clip from the OTIO timeline. Used when a block is re-reconciled
    (its old audio becomes invalid) or when a scene is deleted.
    """
    kind: Literal["delete_from_otio"] = "delete_from_otio"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str
    track_name: Literal["A1_Narration", "V1_Video", "both"]
    reason: str = Field(..., min_length=1)
```

---

### 3.7 Pipeline Effects

Produced by agents and the Provisioner. These effects record pipeline lifecycle events.

#### 3.7.1 PipelineStarted, PipelineComplete, PipelineAborted, VASTGlobalStateObserved

```python
class PipelineStarted(Effect):
    """The parser extracts this from Scenario Agent text to signal that a new pipeline run has begun.

    Agents check for the presence of a PipelineStarted effect
    to determine whether the run has begun.
    """
    kind: Literal["pipeline_started"] = "pipeline_started"
    agent: str = "scenario"
    config: dict = Field(default_factory=dict, description="pipeline configuration snapshot")
    max_tts_budget_usd: float = Field(default=2.0, gt=0.0)
    max_attempts_per_block: int = Field(default=5, ge=1)
    max_run_budget_usd: float = Field(default=10.0, gt=0.0)
    output_path: str = Field(default="/tmp/final_documentary.mp4")


class PipelineComplete(Effect):
    """Assembly finished. Final MP4 validated and ready."""
    kind: Literal["pipeline_complete"] = "pipeline_complete"
    agent: str = "assembly"
    output_path: str
    duration_sec: float = Field(..., ge=0.0)
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    validation_passed: bool = True


class PipelineAborted(Effect):
    """Unrecoverable stop. All agent HTTP services continue running but
    no new effects are emitted for this run.

    Reasons include budget exhaustion, repeated VM provision
    failures beyond threshold, or human instruction to abort.
    """
    kind: Literal["pipeline_aborted"] = "pipeline_aborted"
    agent: str = Field(..., description="agent or component that triggered abort")
    reason: Literal[
        "budget_exceeded",
        "vm_unavailable",
        "human_request",
        "loop_detected",
        "unknown",
    ]
    error_log: list[str] = Field(default_factory=list)
    spent_usd: float = Field(default=0.0, ge=0.0)


class VASTGlobalStateObserved(Effect):
    """Global Vast.ai account state observed by the Provisioner.

    The parser extracts this periodically (e.g. every Provisioner activation) to capture
    account-level data that affects provisioning decisions: credit balance,
    active instance count, and current billing rate. This is not per-run
    state — it is global account telemetry that the operator and agents
    may inspect.

    **Sole ownership:** Only the Provisioner may produce text from which the parser extracts VASTGlobalStateObserved.
    """
    kind: Literal["vast_global_state_observed"] = "vast_global_state_observed"
    agent: str = "provisioner"
    credit_balance_usd: float = Field(default=0.0, description="Current Vast.ai account credit balance")
    active_instance_count: int = Field(default=0, ge=0)
    current_billing_rate_usd_hr: float = Field(default=0.0, ge=0.0)
    observed_at: float = Field(default_factory=time.time)


class BudgetSet(Effect):
    """Run budget established or updated.

    The parser extracts this at run start (from Scenario Agent text) or when operator overrides budget.
    """
    kind: Literal["budget_set"] = "budget_set"
    agent: str = "scenario"
    budget_usd: float = Field(..., gt=0.0)
    reason: str = Field(default="run_start", description="run_start or operator_override")


class BudgetExceeded(Effect):
    """Cumulative spend exceeded the run budget.

    Emitted by the agent handler when CostTracking (from pydantic-ai-shields)
    reports total_cost_usd > budget_usd. Halts new effect generation.
    """
    kind: Literal["budget_exceeded"] = "budget_exceeded"
    spent_usd: float = Field(..., ge=0.0)
    limit_usd: float = Field(..., gt=0.0)
    agent: str = Field(default="handler", description="component that detected exceedance")
```

---

### 3.8 Bash / Human / Fallback Effects

Escape hatches, human intervention requests, and meta-effects that don't fit other families.

#### 3.8.1 HumanInstruction, ClarificationRequest

**V7.1:** `ExecuteRawBash` removed — agents use `bash_command` tool directly.

```python
class HumanInstruction(Effect):
    """Human operator posted a directive to a specific agent.

    The operator POSTs directly to the agent's endpoint with free text. The
    agent parses it on its next turn. Instructions can override parameters,
    approve blocked commands, or redirect the pipeline (e.g. "skip scene 5").

    Instructions are permanent until superseded by another HumanInstruction
    or PipelineAborted. No expiry — Principle 4 prohibits deadline checks.
    """
    kind: Literal["human_instruction"] = "human_instruction"
    target_agent: str = Field(..., description="target agent name or 'all'")
    instruction: str = Field(..., min_length=1)
    from_human: str = Field(..., description="human identifier")
    posted_at: float = Field(default_factory=time.time)
    priority: Literal["normal", "urgent", "blocking"] = "normal"
    action: Literal["budget_override", "emergency_abort", "approve_command", "revoke", "generic"] = "generic"
    action_params: dict = Field(default_factory=dict)

    # V7.1 fix: `target_agent` replaces `agent` to avoid collision with
    # Effect.agent (which names the *producer* component, not the target).
    # All handler code that routes HumanInstruction must use `target_agent`.


class ClarificationRequest(Effect):
    """Parser or agent needs human input to proceed.

    Triggers include: parser confidence below threshold,
    unresolvable VM provision failures, or agent loop detection.
    The pipeline halts (no new effects) until a `HumanInstruction` resolves the
    request.
    """
    kind: Literal["clarification_request"] = "clarification_request"
    target_agent: str = Field(default="human", description="agent that should receive the clarification; usually 'human' for operator routing")
    parser_category: str = ""  # which parser category triggered this (if any)
    raw_text: str = ""  # original agent output that caused the problem
    failure_reason: str = Field(..., description="why clarification is needed")
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    suggested_resolution: str = ""
    question: str = ""           # human-readable question
    referenced_text: str = ""    # text that caused the issue
```

#### 3.8.2 AgentLoopDetected, NoOp

```python
class AgentLoopDetected(Effect):
    """An agent detected it is stuck in a loop.

    pydantic-deep StuckLoopDetection fires this (P14 ADOPT). Three patterns:
    1. Repeated identical tool calls
    2. Alternating A-B-A-B oscillation
    3. No-op calls returning same result

    When fired, the parser extracts `ClarificationRequest` from agent output
    for human review. No automatic recovery — operator intervenes.
    """
    kind: Literal["agent_loop_detected"] = "agent_loop_detected"
    agent: str = Field(..., description="agent that is looping")
    loop_signature: str = Field(..., description="concatenated kind sequence")
    effect_sequence: list[str] = Field(default_factory=list, description="last N effect kinds")
    detection_mode: Literal["duplicate_effects", "alternating", "noop", "both"] = "both"
    detection_count: int = Field(..., ge=1, description="how many times loop pattern repeated")


class NoOp(Effect):
    """Informational effect carrying no state mutation.

    The parser emits NoOp as a fallback when an agent's response contains
    no extractable effects. It is also used for heartbeat pings and logging.
    No projection applies NoOp — it passes through the store untouched.
    """
    kind: Literal["noop"] = "noop"
    reason: str = Field(default="no_effects_extracted")
    agent_context: str = ""  # free-text context from the agent
```


---

### 3.9 Production Failure Effect

Produced by the Audio Agent, Video Agent, or Assembly Agent when media generation or final assembly fails in a way that requires explicit routing.

#### 3.9.1 ProductionFailed with failure_type routing table

```python
class ProductionFailed(Effect):
    """Media production or assembly failure with structured suggested fix.

    The failure_type field is the routing key. Agents read failure_type
    to decide: back-edge to SCRIPT, requeue in current phase, or halt
    for human intervention.
    """
    kind: Literal["production_failed"] = "production_failed"
    failure_type: Literal[
        "overlap",
        "duration_mismatch",
        "gap_unexpected",
        "voice_mismatch",
        "visual_incoherence",
        "artistic_reject",
        "missing_media",
        "invalid_range",
        "track_misalignment",
        "audio_lufs",
    ]
    slot_id: str = ""
    expected: str = ""  # human-readable expected value
    actual: str = ""    # human-readable actual value
    suggested_fix: SuggestedFix = Field(default_factory=SuggestedFix)
    vm_instance_id: str = ""
    attempt_number: int = Field(default=1, ge=1)


class SuggestedFix(BaseModel):
    """Structured fix proposal. Not a top-level effect — embedded in ProductionFailed."""
    fix_type: Literal[
        "requeue",             # retry same job with adjusted params
        "rewrite_script",      # back-edge to SCRIPT, fix narration text
        "adjust_params",       # tweak generation parameters
        "manual_intervention", # halt, human must fix
        "skip",                # skip this block and continue
    ] = "requeue"
    target_scene: int | None = None
    target_block: str | None = None
    new_params: dict = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    retry_count_suggestion: int = Field(default=3, ge=0, description="try N more times then escalate")
    rationale: str = ""
```

**Failure type routing table:**

| failure_type | Routing Action | Target Phase | Rationale |
|---|---|---|---|
| `gap_unexpected` | Back-edge to SCRIPT | SCRIPT | Narration text doesn't fit target duration |
| `voice_mismatch` | Back-edge to SCRIPT | SCRIPT | Wrong speaker or voice role assigned |
| `overlap` | Requeue with adjusted timing | AUDIO_RECONCILE | Clip overlaps neighbor in timeline |
| `duration_mismatch` | Requeue with new params | AUDIO_RECONCILE | TTS output duration outside tolerance |
| `visual_incoherence` | Requeue with revised prompt | VIDEO_PRODUCTION | LTX output doesn't match narration |
| `artistic_reject` | Requeue with adjusted params | VIDEO_PRODUCTION | Quality bar not met |
| `audio_lufs` | Requeue with gain adjustment | AUDIO_RECONCILE | Audio loudness out of spec |
| `track_misalignment` | Requeue assembly | ASSEMBLY | A/V tracks don't align after merge |
| `missing_media` | Retry artifact delivery | current phase | Artifact file not found at expected path |
| `invalid_range` | Requeue with corrected timing | current phase | OTIO source range is invalid |

**Mapping routing actions to SuggestedFix.fix_type:**

| Routing Action | SuggestedFix.fix_type | Notes |
|---|---|---|
| Back-edge to SCRIPT | `rewrite_script` | Scenario Agent rewrites narration |
| Requeue with adjusted timing | `adjust_params` | Tweaks timing parameters |
| Requeue with new params | `requeue` | Default retry behavior |
| Halt for human intervention | `manual_intervention` | Operator takes over |
| Skip block | `skip` | Continue without this block |

The Scenario Agent checks for `failure_type in {"gap_unexpected", "voice_mismatch"}` to trigger the SCRIPT back-edge. All other failure types either requeue in the current phase or halt with `ClarificationRequest`.

---

### 3.10 EffectUnion and KIND_TO_MODEL

#### 3.10.1 KIND_TO_MODEL routing table

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
from typing import Annotated
from pydantic import Field

EffectUnion = Annotated[
    Union[
        # 3.2 Script Effects (3)
        UpdateScript,
        DeleteScene,
        ReorderScenes,
        # 3.3 Job Effects (6)
        QueueJob,
        JobStarted,
        JobCompleted,
        JobFailed,
        JobRequeued,
        JobApproved,
        # 3.4 Reconciliation Effects (6)
        AudioGenerated,
        AudioMeasured,
        DurationAdjusted,
        ReconciliationFailed,
        ReconciliationComplete,
        # 3.5 VM Effects (4)
        VMAllocated,
        VMDeallocated,
        VMProvisionFailed,
        VMObserved,
        # 3.6 OTIO Effects (2)
        MergeIntoOTIO,
        DeleteFromOTIO,
        # 3.7 Pipeline Effects (4)
        PipelineStarted,
        PipelineComplete,
        PipelineAborted,
        VASTGlobalStateObserved,
        # 3.7.2 Budget Effects (2)
        BudgetSet,
        BudgetExceeded,
        # 3.8 Human / Fallback (4)
        HumanInstruction,
        ClarificationRequest,
        AgentLoopDetected,
        NoOp,
        # 3.9 Production Failure (1)
        ProductionFailed,
    ],
    Field(discriminator="kind"),
]
```

`EffectUnion` is the only type accepted by the parser before event store append. Pydantic validates that the `kind` field matches the declared `Literal` value on the subclass. Any JSON payload with an unknown `kind` fails validation at the parser level, before reaching the event store.

#### 3.10.2 Complete KIND_TO_MODEL mapping

The parser uses `KIND_TO_MODEL` to resolve a `kind` string to the correct Pydantic model for validation. This mapping is used in semantic extraction where the LLM outputs a discriminated union with `kind` as the discriminator:

```python
KIND_TO_MODEL: dict[str, type[Effect]] = {
    # 3.2 Script Effects
    "update_script":      UpdateScript,
    "delete_scene":       DeleteScene,
    "reorder_scenes":     ReorderScenes,
    # 3.3 Job Effects
    "queue_job":          QueueJob,
    "job_started":        JobStarted,
    "job_completed":      JobCompleted,
    "job_failed":         JobFailed,
    "job_requeued":       JobRequeued,
    "job_approved":       JobApproved,
    # 3.4 Reconciliation Effects
    "audio_generated":    AudioGenerated,
    "audio_measured":     AudioMeasured,
    "duration_adjusted":  DurationAdjusted,
    "reconciliation_failed":    ReconciliationFailed,
    "reconciliation_complete":  ReconciliationComplete,
    # 3.5 VM Effects
    "vm_allocated":       VMAllocated,
    "vm_deallocated":     VMDeallocated,
    "vm_provision_failed": VMProvisionFailed,
    "vm_observed":        VMObserved,
    # 3.6 OTIO Effects
    "merge_into_otio":    MergeIntoOTIO,
    "delete_from_otio":   DeleteFromOTIO,
    # 3.7 Pipeline Effects
    "pipeline_started":   PipelineStarted,
    "pipeline_complete":  PipelineComplete,
    "pipeline_aborted":   PipelineAborted,
    "vast_global_state_observed":  VASTGlobalStateObserved,
    # 3.7.2 Budget Effects
    "budget_set":         BudgetSet,
    "budget_exceeded":    BudgetExceeded,
    # 3.8 Human / Fallback
    "human_instruction":  HumanInstruction,
    "clarification_request": ClarificationRequest,
    "agent_loop_detected":   AgentLoopDetected,
    "noop":               NoOp,
    # 3.9 Production Failure
    "production_failed":  ProductionFailed,
}
```

The `_EffectUnion` discriminated union (§9.5) uses `kind` as the discriminator. Instructor constrains the LLM to output only valid `kind` values from this union. Pydantic validates the full payload against the corresponding model. See [[09.5 - Effect Parser Semantic Extraction Pipeline|§9.5]] for the complete semantic extraction pipeline.

#### 3.10.3 Naming convention summary

| Convention | Pattern | Examples |
|---|---|---|
| Imperative (agent requests) | Verb-noun, present tense | `QueueJob`, `MergeIntoOTIO`, `DeleteScene` |
| Past-tense (system outcomes) | Noun-verb or noun-adjective, past tense | `JobCompleted`, `AudioMeasured`, `PipelineComplete`, `VMDeallocated` |
| State descriptors | Adjective or participle | `ReconciliationComplete`, `VMObserved` |
| Meta / diagnostic | Descriptive phrase | `AgentLoopDetected`, `ClarificationRequest`, `ProductionFailed` |

The naming convention is enforced by code review, not by the type system. When adding a new effect type, place it in the family section matching its producer, follow the naming convention based on whether it is an agent request or a system outcome, add it to `EffectUnion`, and register it in `KIND_TO_MODEL`.


---

