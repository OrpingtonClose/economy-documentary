from __future__ import annotations

import time
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field
from uuid_utils import uuid7




class Effect(BaseModel):
    """Base for all effect types. NEVER instantiated directly.

    Fields present on every effect emitted into the event store:
    - run_id:        pipeline run identifier (opaque string)
    - effect_id:     UUIDv7 generated client-side for idempotent retries
    - kind:          Literal discriminant string (overridden per subclass)
    - agent:         component that produced the effect (e.g. "scenario")
    - timestamp:     seconds since epoch at creation time

    The EventStore deduplicates on (run_id, effect_id) via INSERT OR IGNORE
    so that retrying an append with the same effect_id is a safe no-op.
    """
    run_id: str
    effect_id: UUID = Field(default_factory=uuid7)
    kind: str = "effect"  # overridden per subclass via Literal
    agent: str
    timestamp: float = Field(default_factory=time.time)

class UpdateScript(Effect):
    """Write or revise a scene's narration slot.

    Each UpdateScript defines one narration block: speaker, text content,
    timing target, and optional production hints. The OTIOProjection merges
    this into the A1_Narration track, creating or overwriting the slot for
    (scene_num, block_id).
    """
    kind: Literal["update_script"] = "update_script"
    scene_num: int = Field(..., ge=1, description="1-based scene index")
    block_id: str = Field(..., description="stable identifier for this narration block")
    speaker: str = Field(..., description="voice role: narrator, guest_a, etc.")
    text: str = Field(..., min_length=1, description="narration text")
    pronunciation_hints: list[str] = Field(default_factory=list)
    visual_notes: str = ""
    dopamine_hook: str = ""
    duration_sec: float = Field(..., gt=0.0, description="target duration in seconds")


class DeleteScene(Effect):
    """Remove a scene and all its narration blocks from the timeline."""
    kind: Literal["delete_scene"] = "delete_scene"
    scene_num: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1)


class ReorderScenes(Effect):
    """Change scene order. new_order[i] is the scene_num that should occupy position i+1.

    The OTIOProjection resequences the top-level timeline tracks so that
    scene N moves to the position specified. All narration and video slots
    attached to a scene move with it.
    """
    kind: Literal["reorder_scenes"] = "reorder_scenes"
    new_order: list[int] = Field(..., min_length=1)

class QueueJob(Effect):
    """Demand creation of a media artifact by a VM worker.

    The Provisioner reads QueueJob from the JobProjection and matches it to
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


class JobCompleted(Effect):
    """VM worker finished successfully; artifact is ready for quality review."""
    kind: Literal["job_completed"] = "job_completed"
    job_id: str
    artifact_path: str = Field(..., description="absolute path to generated file")
    duration_sec: float = Field(..., ge=0.0, description="actual media duration")
    vm_instance_id: str


class JobFailed(Effect):
    """VM worker failed. failure_category drives retry vs escalation policy."""
    kind: Literal["job_failed"] = "job_failed"
    job_id: str
    error_message: str
    failure_category: Literal[
        "oom",           # GPU out of memory
        "timeout",       # VM-side timeout (legacy; see TimeoutObserved for pipeline-side)
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

    The Audio or Video Agent emits this when a JobCompleted artifact fails
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
    artifact_path: str
    quality_notes: str = ""
    reviewed_by: str = Field(default="agent", description="'agent' or human name")

class AudioGenerated(Effect):
    """TTS WAV produced by a VM worker. Artifact awaits WhisperX measurement."""
    kind: Literal["audio_generated"] = "audio_generated"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    artifact_path: str


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
    whisperx_confidence: float = Field(..., ge=0.0, le=1.0)


class DurationAdjusted(Effect):
    """Measured duration within tolerance; OTIO slot updated with authoritative value.

    The Audio Agent computes delta = measured_sec - scripted_sec. If
    |delta| <= max(15% * scripted_sec, 0.25s) the block passes. The OTIO
    Projection updates the slot's source_range to match measured_sec.
    """
    kind: Literal["duration_adjusted"] = "duration_adjusted"
    block_id: str
    scene_num: int
    voice_role: str
    scripted_sec: float
    measured_sec: float
    delta_sec: float
    tolerance_sec: float

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


class ReconciliationPartial(Effect):
    """After a script back-edge, some blocks remain authoritative while others
    are marked dirty and must be re-reconciled.

    The Audio Agent computes the dirty set by comparing the new script against
    the authoritative OTIO: blocks whose text, speaker, or duration target
    changed are dirty; all others keep their AudioMeasured values.
    """
    kind: Literal["reconciliation_partial"] = "reconciliation_partial"
    agent: str
    dirty_block_ids: list[str] = Field(default_factory=list)
    clean_block_ids: list[str] = Field(default_factory=list)
    reason: str = Field(..., description="e.g. 'voice_mismatch back-edge from VIDEO_PRODUCTION'")
    blocks_dirty: int
    blocks_clean: int


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

class VMAllocated(Effect):
    """GPU instance created and ready for job assignment.

    The Provisioner emits this after successfully creating a Vast.ai instance
    and verifying that the worker HTTP endpoint (port 9000+) responds to GET /.
    """
    kind: Literal["vm_allocated"] = "vm_allocated"
    instance_id: str = Field(..., description="Vast.ai instance ID")
    role: Literal["tts", "ltx"] = Field(..., description="worker role determines GPU type")
    offer_id: str = Field(..., description="Vast.ai offer ID that was accepted")
    worker_url: str = Field(..., description="full URL including port, e.g. http://1.2.3.4:9000")
    gpu_type: str = Field(..., description="GPU model, e.g. 'RTX 4090'")
    cost_per_hour: float = Field(..., gt=0.0)
    vm_login_name: str = "root"


class VMDeallocated(Effect):
    """GPU instance destroyed. Final cost is recorded for budget tracking."""
    kind: Literal["vm_deallocated"] = "vm_deallocated"
    instance_id: str
    reason: Literal[
        "job_done",       # worker finished all assigned jobs
        "cost_limit",     # exceeded per-VM cost threshold
        "stale",          # TimeoutObserved triggered cleanup
        "provision_failed",  # never reached healthy state
        "manual",         # human overseer destroyed via instruction
    ]
    final_cost: float = Field(default=0.0, ge=0.0)
    runtime_sec: float = Field(default=0.0, ge=0.0)


class VMProvisionFailed(Effect):
    """Provisioner could not create a VM for a pending job.

    On repeated failures (configurable threshold, default 3), the Provisioner
    halts and emits `ClarificationRequest` for human intervention. It does not
    attempt creative recovery — deterministic lackey behavior.
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
    instance status against the VMProjection's internal model. When drift is
    detected, it emits VMObserved so the projection can reconcile.
    """
    kind: Literal["vm_observed"] = "vm_observed"
    instance_id: str
    observed_status: Literal[
        "running", "offline", "not_found", "unknown"
    ] = Field(..., description="what Vast.ai API reports")
    expected_status: Literal[
        "running", "offline", "not_found", "unknown"
    ] = Field(..., description="what VMProjection believes")
    drift_description: str = Field(..., description="human-readable drift summary")
    corrective_action: Literal[
        "none",           # minor drift, no action
        "deallocate",     # VM gone from Vast.ai, clean up projection
        "refresh_state",  # transient, re-poll next cycle
        "escalate",       # unresolvable, emit ClarificationRequest
    ] = "none"

class MergeIntoOTIO(Effect):
    """Approved clip enters the OTIO timeline at the specified track and slot.

    The OTIOProjection creates (or replaces) an `otio.schema.Clip` with a
    media reference pointing to `artifact_path` and a source range of
    `duration_sec`. The clip is placed on `track_name` at `start_time`.
    """
    kind: Literal["merge_into_otio"] = "merge_into_otio"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str = Field(..., description="OTIO slot identifier")
    artifact_path: str
    track_name: Literal["A1_Narration", "V1_Video"] = Field(..., description="audio or video track")
    start_time: float = Field(..., ge=0.0, description="timeline start in seconds")
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

class PipelineStarted(Effect):
    """Launcher emitted this to signal that a new pipeline run has begun.

    Agents check for the presence of a PipelineStarted effect
    to determine whether the run has begun.
    """
    kind: Literal["pipeline_started"] = "pipeline_started"
    agent: str = "launcher"
    config: dict = Field(default_factory=dict, description="pipeline configuration snapshot")
    max_tts_budget_usd: float = Field(default=2.0, gt=0.0)
    max_attempts_per_block: int = Field(default=5, ge=1)
    max_run_budget_usd: float = Field(default=10.0, gt=0.0)
    output_path: str = Field(default="/tmp/final_documentary.mp4")


class PipelineComplete(Effect):
    """Assembly finished. Final MP4 validated and ready."""
    kind: Literal["pipeline_complete"] = "pipeline_complete"
    agent: str = "assembly"
    run_id: str  # duplicate of base field for convenience in queries
    output_path: str
    duration_sec: float = Field(..., ge=0.0)
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    validation_passed: bool = True


class PipelineAborted(Effect):
    """Unrecoverable stop. The watcher loop ceases sending ticks.

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


class TimeoutObserved(Effect):
    """Watcher detected a job pending longer than threshold.

    The watcher loop checks JobProjection on every tick: if any job has
    status "pending" or "running" for more than `stale_threshold_min`,
    this effect is emitted. The Provisioner routes it to VM cleanup
    (deallocates the stuck VM) + job requeue.

    This is a projection-based observation — no `threading.Timer`, no
    `asyncio.timeout`. Principle 4 ("no timeouts") is preserved because
    the timeout is observed externally, not enforced by the VM.
    """
    kind: Literal["timeout_observed"] = "timeout_observed"
    agent: str = "watcher"
    job_id: str
    vm_instance_id: str = ""
    pending_since: float = Field(..., description="timestamp when job entered pending/running")
    elapsed_min: float = Field(..., gt=0.0, description="how long the job has been pending")
    stale_threshold_min: float = Field(default=10.0, gt=0.0)
    action_taken: Literal["deallocate_vm", "requeue_job", "escalate"] = "deallocate_vm"

class ExecuteRawBash(Effect):
    """Escape hatch: run a shell command.

    Security model: pre-approved commands (`ffmpeg`, `ffprobe`,
    `whisperx`, `vastai`, `python3` with known scripts) run without gating.
    Non-allowlisted commands are replaced by the parser with a
    `ClarificationRequest` ("Agent wants to run `curl ...` — approve?").
    Human approval produces a `HumanInstruction` containing the cleared command.

    The `approved_by_human` flag is set only after explicit human approval.
    Commands without this flag are rejected by the execution handler.
    """
    kind: Literal["execute_raw_bash"] = "execute_raw_bash"
    command: str = Field(..., min_length=1)
    working_dir: str = "/tmp"
    approved_by_human: bool = False
    approved_by: str = ""  # human name or empty
    allowlisted: bool = Field(
        default=False,
        description="True if command matches ALLOWLISTED_COMMANDS in config",
    )
    expected_artifacts: list[str] = Field(
        default_factory=list,
        description="files this command is expected to produce",
    )


class HumanInstruction(Effect):
    """Human overseer posted a directive to a specific agent.

    The overseer POSTs to the agent's endpoint with free text. The agent
    parses it on its next turn. Instructions can override parameters, approve
    blocked commands, or redirect the pipeline (e.g. "skip scene 5").
    """
    kind: Literal["human_instruction"] = "human_instruction"
    agent: str = Field(..., description="target agent name or 'all'")
    instruction: str = Field(..., min_length=1)
    from_human: str = Field(..., description="human identifier")
    posted_at: float = Field(default_factory=time.time)
    priority: Literal["normal", "urgent", "blocking"] = "normal"
    expires_at: float | None = None  # if set, instruction is ignored after this timestamp
    action: Literal["budget_override", "emergency_abort", "approve_command", "generic"] = "generic"
    action_params: dict = Field(default_factory=dict)


class ClarificationRequest(Effect):
    """Parser or agent needs human input to proceed.

    Triggers include: parser confidence below threshold, non-allowlisted
    bash command, unresolvable VM provision failures, or agent loop detection.
    The pipeline halts (no ticks) until a `HumanInstruction` resolves the
    request.
    """
    kind: Literal["clarification_request"] = "clarification_request"
    agent: str = Field(default="overseer", description="usually 'overseer' for human routing")
    parser_category: str = ""  # which parser category triggered this (if any)
    raw_text: str = ""  # original agent output that caused the problem
    failure_reason: str = Field(..., description="why clarification is needed")
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    suggested_resolution: str = ""
    question: str = ""           # human-readable question
    referenced_text: str = ""    # text that caused the issue

class AgentLoopDetected(Effect):
    """Watcher detected an agent stuck in a loop.

    Two independent detection modes, both with configurable threshold N
    (default 5):
    1. Duplicate effects: the last N effects from this agent are identical.
    2. No progress: N ticks have elapsed without any projection state change.

    When either fires, the watcher halts and emits `ClarificationRequest`
    for human review.
    """
    kind: Literal["agent_loop_detected"] = "agent_loop_detected"
    agent: str = Field(..., description="agent that is looping")
    loop_signature: str = Field(..., description="concatenated kind sequence, e.g. 'queue_job:queue_job:queue_job'")
    effect_sequence: list[str] = Field(default_factory=list, description="last N effect kinds")
    detection_mode: Literal["duplicate_effects", "no_progress", "both"] = "both"
    detection_count: int = Field(..., ge=1, description="how many times loop pattern repeated")
    threshold: int = Field(default=5, ge=1, description="configured threshold that was exceeded")
    projection_delta: dict = Field(
        default_factory=dict,
        description="snapshot of projection changes (empty = no progress)",
    )


class NoOp(Effect):
    """Informational effect carrying no state mutation.

    The parser emits NoOp as a fallback when an agent's response contains
    no extractable effects. It is also used for heartbeat pings and logging.
    No projection applies NoOp — it passes through the store untouched.
    """
    kind: Literal["noop"] = "noop"
    reason: str = Field(default="no_effects_extracted")
    agent_context: str = ""  # free-text context from the agent

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



from typing import Annotated
from pydantic import Field

EffectUnion = Annotated[
    Union[
        # 3.2 Script Effects (3)
        UpdateScript,
        DeleteScene,
        ReorderScenes,
        # 3.3 Job Effects (5)
        QueueJob,
        JobCompleted,
        JobFailed,
        JobRequeued,
        JobApproved,
        # 3.4 Reconciliation Effects (6)
        AudioGenerated,
        AudioMeasured,
        DurationAdjusted,
        ReconciliationFailed,
        ReconciliationPartial,
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
        TimeoutObserved,
        # 3.8 Bash / Human / Fallback (5)
        ExecuteRawBash,
        HumanInstruction,
        ClarificationRequest,
        AgentLoopDetected,
        NoOp,
        # 3.9 Production Failure (1)
        ProductionFailed,
    ],
    Field(discriminator="kind"),
]

KIND_TO_MODEL: dict[str, type[Effect]] = {
    # 3.2 Script Effects
    "update_script":      UpdateScript,
    "delete_scene":       DeleteScene,
    "reorder_scenes":     ReorderScenes,
    # 3.3 Job Effects
    "queue_job":          QueueJob,
    "job_completed":      JobCompleted,
    "job_failed":         JobFailed,
    "job_requeued":       JobRequeued,
    "job_approved":       JobApproved,
    # 3.4 Reconciliation Effects
    "audio_generated":    AudioGenerated,
    "audio_measured":     AudioMeasured,
    "duration_adjusted":  DurationAdjusted,
    "reconciliation_failed":    ReconciliationFailed,
    "reconciliation_partial":   ReconciliationPartial,
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
    "timeout_observed":   TimeoutObserved,
    # 3.8 Bash / Human / Fallback
    "execute_raw_bash":   ExecuteRawBash,
    "human_instruction":  HumanInstruction,
    "clarification_request": ClarificationRequest,
    "agent_loop_detected":   AgentLoopDetected,
    "noop":               NoOp,
    # 3.9 Production Failure
    "production_failed":  ProductionFailed,
}