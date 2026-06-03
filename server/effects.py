# pyright: reportIncompatibleVariableOverride=false
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Annotated, Literal, Union, List, Dict
from uuid import UUID
from pydantic import BaseModel, Field, field_validator, model_validator
import uuid
from uuid_utils import uuid7 as _uuid7

def uuid7() -> uuid.UUID:
    return uuid.UUID(bytes=_uuid7().bytes)


def parse_duration(val: Any) -> float:
    """Parse standard durations (e.g., floats/integers) and time formats like "MM:SS" or "HH:MM:SS" to float.

    Examples:
        "2:30" -> 150.0
        "1:02:30" -> 3750.0
        15.5 -> 15.5
        "15.5" -> 15.5
    """
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        if ":" in val:
            parts = val.split(":")
            try:
                if len(parts) == 2:
                    m = int(parts[0])
                    s = float(parts[1])
                    return m * 60.0 + s
                elif len(parts) == 3:
                    h = int(parts[0])
                    m = int(parts[1])
                    s = float(parts[2])
                    return h * 3600.0 + m * 60.0 + s
            except ValueError:
                pass
        try:
            return float(val)
        except ValueError:
            raise ValueError(f"Could not parse duration string: {val}")
    raise ValueError(f"Invalid duration type: {type(val)}")


class Effect(BaseModel):
    """Base for all effect types. NEVER instantiated directly.

    Fields present on every effect emitted into the event store:
    - effect_id:     UUIDv7 generated client-side for idempotent retries
    - kind:          Literal discriminant string (overridden per subclass)
    - agent:         component that produced the effect (e.g. "scenario")
    - timestamp:     seconds since epoch at creation time
    """
    effect_id: UUID = Field(default_factory=uuid7)
    kind: str = "effect"  # overridden per subclass via Literal
    agent: str
    timestamp: float = Field(default_factory=time.time)


# ===========================================================================
# 3.2 Script Effects
# ===========================================================================

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

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _parse_duration_sec(cls, val: Any) -> float:
        return parse_duration(val)


class UpdateScript(Effect):
    """Write or revise one or more scene narration blocks."""
    kind: Literal["update_script"] = "update_script"
    blocks: list[ScriptBlock] = Field(..., min_length=1)


class DeleteScene(Effect):
    """Remove a scene and all its narration blocks from the timeline."""
    kind: Literal["delete_scene"] = "delete_scene"
    scene_num: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1)


class ReorderScenes(Effect):
    """Change scene order. new_order[i] is the scene_num that should occupy position i+1."""
    kind: Literal["reorder_scenes"] = "reorder_scenes"
    new_order: list[int] = Field(..., min_length=1)


# ===========================================================================
# 3.3 Job Effects
# ===========================================================================

class QueueJob(Effect):
    """Demand creation of a media artifact by a VM worker."""
    kind: str = "queue_job"
    job_id: str = Field(..., description="stable unique job identifier")
    job_type: str = Field(..., description="tts or ltx")
    scene_num: int = Field(..., ge=1)
    block_id: str
    slot_id: str = Field(..., description="OTIO slot where the result belongs")
    params: dict = Field(default_factory=dict, description="type-specific generation params")


class QueueAudioJob(QueueJob):
    kind: Literal["queue_audio_job"] = "queue_audio_job"
    job_type: Literal["tts"] = "tts"


class QueueVideoJob(QueueJob):
    kind: Literal["queue_video_job"] = "queue_video_job"
    job_type: Literal["ltx"] = "ltx"


class JobStarted(Effect):
    """VM worker accepted the job. Job is now running."""
    kind: str = "job_started"
    job_id: str
    vm_instance_id: str
    started_at: float = Field(default_factory=time.time)


class AudioJobStarted(JobStarted):
    kind: Literal["audio_job_started"] = "audio_job_started"


class VideoJobStarted(JobStarted):
    kind: Literal["video_job_started"] = "video_job_started"


class JobCompleted(Effect):
    """VM worker finished successfully; artifact is ready for quality review."""
    kind: str = "job_completed"
    job_id: str
    artifact_uri: str = Field(..., description="URI to generated file")
    duration_sec: float = Field(..., ge=0.0, description="actual media duration")

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _parse_duration_sec(cls, val: Any) -> float:
        return parse_duration(val)
    vm_instance_id: str
    measurements: list[float] = Field(
        default_factory=list,
        description="WhisperX measurements from VM worker (3 runs)",
    )


class AudioJobCompleted(JobCompleted):
    kind: Literal["audio_job_completed"] = "audio_job_completed"


class VideoJobCompleted(JobCompleted):
    kind: Literal["video_job_completed"] = "video_job_completed"


class JobFailed(Effect):
    """VM worker failed."""
    kind: str = "job_failed"
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


class AudioJobFailed(JobFailed):
    kind: Literal["audio_job_failed"] = "audio_job_failed"


class VideoJobFailed(JobFailed):
    kind: Literal["video_job_failed"] = "video_job_failed"


class JobRequeued(Effect):
    """Artistry rejection: previous output did not meet quality bar."""
    kind: str = "job_requeued"
    job_id: str
    reason: str = Field(..., min_length=1, description="why the previous attempt was rejected")
    new_params: dict | None = None


class AudioJobRequeued(JobRequeued):
    kind: Literal["audio_job_requeued"] = "audio_job_requeued"


class VideoJobRequeued(JobRequeued):
    kind: Literal["video_job_requeued"] = "video_job_requeued"


class JobApproved(Effect):
    """Artistry approval: artifact passes quality review, ready for OTIO merge."""
    kind: str = "job_approved"
    job_id: str
    artifact_uri: str
    quality_notes: str = ""
    reviewed_by: str = Field(default="agent", description="'agent' or human name")


class AudioJobApproved(JobApproved):
    kind: Literal["audio_job_approved"] = "audio_job_approved"


class VideoJobApproved(JobApproved):
    kind: Literal["video_job_approved"] = "video_job_approved"


# ===========================================================================
# 3.4 Reconciliation Effects
# ===========================================================================

class ReconciliationFailureDetail(BaseModel):
    """Per-block failure diagnostic embedded in ReconciliationFailed."""
    block_id: str
    scene_num: int
    phrase_idx: int = Field(..., description="index of phrase within block")
    voice: str
    scripted_sec: float
    measured_sec: float

    @field_validator("scripted_sec", "measured_sec", mode="before")
    @classmethod
    def _parse_secs(cls, val: Any) -> float:
        return parse_duration(val)
    delta_sec: float
    ratio: float = Field(..., description="measured / scripted")
    message: str = Field(..., description="human-readable diagnostic")
    attempt_number: int = Field(default=1, ge=1, description="which reconciliation attempt this was")


class AudioGenerated(Effect):
    """TTS WAV produced by a VM worker. Artifact awaits WhisperX measurement."""
    kind: Literal["audio_generated"] = "audio_generated"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    artifact_uri: str


class AudioMeasured(Effect):
    """WhisperX measured the actual spoken duration of a generated WAV."""
    kind: Literal["audio_measured"] = "audio_measured"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    measured_sec: float = Field(..., description="median of measurements")

    @field_validator("measured_sec", mode="before")
    @classmethod
    def _parse_measured_sec(cls, val: Any) -> float:
        return parse_duration(val)
    measurements: list[float] = Field(
        default_factory=list,
        description="all three WhisperX measurements, unsorted",
    )
    whisperx_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DurationAdjusted(Effect):
    """Measured duration within tolerance; OTIO slot updated with authoritative value."""
    kind: Literal["duration_adjusted"] = "duration_adjusted"
    block_id: str
    slot_id: str = Field(..., description="Canonical slot address, e.g. 'A1:3:2'")
    scene_num: int
    voice_role: str
    scripted_sec: float
    measured_sec: float

    @field_validator("scripted_sec", "measured_sec", mode="before")
    @classmethod
    def _parse_secs(cls, val: Any) -> float:
        return parse_duration(val)


class ReconciliationFailed(Effect):
    """One or more blocks failed the tolerance check. Retry or escalate."""
    kind: Literal["reconciliation_failed"] = "reconciliation_failed"
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    failures: list[ReconciliationFailureDetail] = Field(default_factory=list)
    worst_delta_sec: float
    suggested_adjustments: list[dict] = Field(default_factory=list)
    failure_type: Literal["duration_mismatch", "duration_unrecoverable"] = "duration_mismatch"


class ReconciliationComplete(Effect):
    """All narration blocks pass tolerance. OTIO is now authoritative."""
    kind: Literal["reconciliation_complete"] = "reconciliation_complete"
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    worst_delta_sec: float
    total_measured_sec: float


# ===========================================================================
# 3.5 VM Effects
# ===========================================================================

class VMAllocated(Effect):
    """GPU instance created and ready for job assignment."""
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
    """Provisioner could not create a VM for a pending job."""
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
    """Provisioner detected drift between event-derived VM state and Vast.ai reality."""
    kind: Literal["vm_observed"] = "vm_observed"
    instance_id: str
    observed_status: Literal["running", "offline", "not_found", "unknown"] = Field(..., description="what Vast.ai API reports")
    expected_status: Literal["running", "offline", "not_found", "unknown"] = Field(..., description="what VMs believe")
    drift_description: str = Field(..., description="human-readable drift summary")
    corrective_action: Literal["none", "escalate"] = "none"


# ===========================================================================
# 3.6 OTIO Effects
# ===========================================================================

class MergeIntoOTIO(Effect):
    """Approved clip enters the OTIO timeline at the specified track and slot."""
    kind: Literal["merge_into_otio"] = "merge_into_otio"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str = Field(..., description="Canonical slot address, e.g. 'A1:3:2'")
    artifact_uri: str = Field(..., description="URI to the approved media file")
    track_name: Literal["A1_Narration", "V1_Video"] = Field(..., description="Target track")
    duration_sec: float = Field(..., gt=0.0)

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _parse_duration_sec(cls, val: Any) -> float:
        return parse_duration(val)
    transition_type: Literal["cut", "dissolve", "none"] = "cut"
    transition_duration_sec: float = Field(default=0.0, ge=0.0)
    start_sec: float = Field(default=0.0, description="Optional start time coordinate for coordinate-based schema")


class DeleteFromOTIO(Effect):
    """Remove a clip from the OTIO timeline."""
    kind: Literal["delete_from_otio"] = "delete_from_otio"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str
    track_name: Literal["A1_Narration", "V1_Video", "both"]
    reason: str = Field(..., min_length=1)


# ===========================================================================
# 3.7 Pipeline Effects
# ===========================================================================

class PipelineStarted(Effect):
    """Signal that a new pipeline run has begun."""
    kind: Literal["pipeline_started"] = "pipeline_started"
    config: dict = Field(default_factory=dict, description="pipeline configuration snapshot")
    max_tts_budget_usd: float = Field(default=2.0, gt=0.0)
    max_attempts_per_block: int = Field(default=5, ge=1)
    max_run_budget_usd: float = Field(default=10.0, gt=0.0)
    output_path: str = Field(default="/tmp/final_documentary.mp4")


class PipelineComplete(Effect):
    """Assembly finished. Final MP4 validated and ready."""
    kind: Literal["pipeline_complete"] = "pipeline_complete"
    output_path: str
    duration_sec: float = Field(..., ge=0.0)

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _parse_duration_sec(cls, val: Any) -> float:
        return parse_duration(val)
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    validation_passed: bool = True


class PipelineAborted(Effect):
    """Unrecoverable stop."""
    kind: Literal["pipeline_aborted"] = "pipeline_aborted"
    reason: Literal["budget_exceeded", "vm_unavailable", "human_request", "loop_detected", "unknown"]
    error_log: list[str] = Field(default_factory=list)
    spent_usd: float = Field(default=0.0, ge=0.0)


class VASTGlobalStateObserved(Effect):
    """Global Vast.ai account state observed by the Provisioner."""
    kind: Literal["vast_global_state_observed"] = "vast_global_state_observed"
    credit_balance_usd: float = Field(default=0.0, description="Current Vast.ai credit balance")
    active_instance_count: int = Field(default=0, ge=0)
    current_billing_rate_usd_hr: float = Field(default=0.0, ge=0.0)
    observed_at: float = Field(default_factory=time.time)


class BudgetSet(Effect):
    """Run budget established or updated."""
    kind: Literal["budget_set"] = "budget_set"
    budget_usd: float = Field(..., gt=0.0)
    reason: str = Field(default="run_start", description="run_start or operator_override")


class BudgetExceeded(Effect):
    """Cumulative spend exceeded the run budget."""
    kind: Literal["budget_exceeded"] = "budget_exceeded"
    spent_usd: float = Field(..., ge=0.0)
    limit_usd: float = Field(..., gt=0.0)


# ===========================================================================
# 3.8 Human / Fallback
# ===========================================================================

class HumanInstruction(Effect):
    """Human operator posted a directive to a specific agent."""
    kind: Literal["human_instruction"] = "human_instruction"
    target_agent: str = Field(..., description="target agent name or 'all'")
    instruction: str = Field(..., min_length=1)
    from_human: str = Field(..., description="human identifier")
    posted_at: float = Field(default_factory=time.time)
    priority: Literal["normal", "urgent", "blocking"] = "normal"
    action: Literal["budget_override", "emergency_abort", "approve_command", "revoke", "generic"] = "generic"
    action_params: dict = Field(default_factory=dict)


class ClarificationRequest(Effect):
    """Parser or agent needs human input to proceed."""
    kind: Literal["clarification_request"] = "clarification_request"
    target_agent: str = Field(default="human", description="target agent")
    parser_category: str = ""
    raw_text: str = ""
    failure_reason: str = Field(..., description="why clarification is needed")
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    suggested_resolution: str = ""
    question: str = ""
    referenced_text: str = ""


class AgentLoopDetected(Effect):
    """An agent detected it is stuck in a loop."""
    kind: Literal["agent_loop_detected"] = "agent_loop_detected"
    agent: str = Field(..., description="agent that is looping")
    loop_signature: str = Field(..., description="concatenated kind sequence")
    effect_sequence: list[str] = Field(default_factory=list, description="last N effect kinds")
    detection_mode: Literal["duplicate_effects", "alternating", "noop", "both"] = "both"
    detection_count: int = Field(..., ge=1, description="loop count")


class NoOp(Effect):
    """Informational effect carrying no state mutation."""
    kind: Literal["noop"] = "noop"
    reason: str = Field(default="no_effects_extracted")
    agent_context: str = ""


# ===========================================================================
# 3.9 Production Failure
# ===========================================================================

class SuggestedFix(BaseModel):
    """Structured fix proposal."""
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
    retry_count_suggestion: int = Field(default=3, ge=0, description="try N times")
    rationale: str = ""


class ProductionFailed(Effect):
    """Media production or assembly failure with structured suggested fix."""
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
    expected: str = ""
    actual: str = ""
    suggested_fix: SuggestedFix = Field(default_factory=SuggestedFix)
    vm_instance_id: str = ""
    attempt_number: int = Field(default=1, ge=1)


class MeasurementRequested(Effect):
    """Demand WhisperX measurement of generated audio clip."""
    kind: Literal["measurement_requested"] = "measurement_requested"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str


class VideoMeasured(Effect):
    """Ffprobe or similar measured the actual duration of generated video clip."""
    kind: Literal["video_measured"] = "video_measured"
    job_id: str
    block_id: str
    measured_sec: float

    @field_validator("measured_sec", mode="before")
    @classmethod
    def _parse_measured_sec(cls, val: Any) -> float:
        return parse_duration(val)


# ===========================================================================
# 3.10 EffectUnion and KIND_TO_MODEL
# ===========================================================================

EffectUnion = Annotated[
    Union[
        UpdateScript,
        DeleteScene,
        ReorderScenes,
        QueueJob,
        QueueAudioJob,
        QueueVideoJob,
        JobStarted,
        AudioJobStarted,
        VideoJobStarted,
        JobCompleted,
        AudioJobCompleted,
        VideoJobCompleted,
        JobFailed,
        AudioJobFailed,
        VideoJobFailed,
        JobRequeued,
        AudioJobRequeued,
        VideoJobRequeued,
        JobApproved,
        AudioJobApproved,
        VideoJobApproved,
        AudioGenerated,
        AudioMeasured,
        DurationAdjusted,
        ReconciliationFailed,
        ReconciliationComplete,
        VMAllocated,
        VMDeallocated,
        VMProvisionFailed,
        VMObserved,
        MergeIntoOTIO,
        DeleteFromOTIO,
        PipelineStarted,
        PipelineComplete,
        PipelineAborted,
        VASTGlobalStateObserved,
        BudgetSet,
        BudgetExceeded,
        HumanInstruction,
        ClarificationRequest,
        AgentLoopDetected,
        NoOp,
        ProductionFailed,
        MeasurementRequested,
        VideoMeasured,
    ],
    Field(discriminator="kind"),
]

KIND_TO_MODEL: dict[str, type[Effect]] = {
    "update_script":      UpdateScript,
    "delete_scene":       DeleteScene,
    "reorder_scenes":     ReorderScenes,
    "queue_job":          QueueJob,
    "queue_audio_job":    QueueAudioJob,
    "queue_video_job":    QueueVideoJob,
    "job_started":        JobStarted,
    "audio_job_started":  AudioJobStarted,
    "video_job_started":  VideoJobStarted,
    "job_completed":      JobCompleted,
    "audio_job_completed": AudioJobCompleted,
    "video_job_completed": VideoJobCompleted,
    "job_failed":         JobFailed,
    "audio_job_failed":   AudioJobFailed,
    "video_job_failed":   VideoJobFailed,
    "job_requeued":       JobRequeued,
    "audio_job_requeued": AudioJobRequeued,
    "video_job_requeued": VideoJobRequeued,
    "job_approved":       JobApproved,
    "audio_job_approved": AudioJobApproved,
    "video_job_approved": VideoJobApproved,
    "audio_generated":    AudioGenerated,
    "audio_measured":     AudioMeasured,
    "duration_adjusted":  DurationAdjusted,
    "reconciliation_failed":    ReconciliationFailed,
    "reconciliation_complete":  ReconciliationComplete,
    "vm_allocated":       VMAllocated,
    "vm_deallocated":     VMDeallocated,
    "vm_provision_failed": VMProvisionFailed,
    "vm_observed":        VMObserved,
    "merge_into_otio":    MergeIntoOTIO,
    "delete_from_otio":   DeleteFromOTIO,
    "pipeline_started":   PipelineStarted,
    "pipeline_complete":  PipelineComplete,
    "pipeline_aborted":   PipelineAborted,
    "vast_global_state_observed":  VASTGlobalStateObserved,
    "budget_set":         BudgetSet,
    "budget_exceeded":    BudgetExceeded,
    "human_instruction":  HumanInstruction,
    "clarification_request": ClarificationRequest,
    "agent_loop_detected":   AgentLoopDetected,
    "noop":               NoOp,
    "production_failed":  ProductionFailed,
    "measurement_requested": MeasurementRequested,
    "video_measured":     VideoMeasured,
}
