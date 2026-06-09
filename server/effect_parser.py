# pyright: reportIncompatibleVariableOverride=false
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Literal, Union, Annotated, List, Dict
import logging
from pydantic import BaseModel, Field, field_validator, model_validator
import instructor
from openai import AsyncOpenAI

from effects import (
    Effect,
    UpdateScript,
    DeleteScene,
    ReorderScenes,
    QueueJob,
    JobStarted,
    JobCompleted,
    JobFailed,
    JobRequeued,
    JobApproved,
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
    ScriptBlock,
    ReconciliationFailureDetail,
    SuggestedFix,
    KIND_TO_MODEL,
    parse_duration,
)

logger = logging.getLogger(__name__)

# ===========================================================================
# Parser Models (Omitting run_id, effect_id, agent, timestamp)
# ===========================================================================

class _NoOpEffect(BaseModel):
    kind: Literal["noop"] = "noop"
    reason: str = "no_effects_extracted"
    agent_context: str = ""


class _UpdateScriptEffect(BaseModel):
    kind: Literal["update_script"] = "update_script"
    blocks: list[ScriptBlock] = Field(..., min_length=1)

    @field_validator("blocks", mode="before")
    @classmethod
    def _blocks_must_have_text(cls, v: Any) -> Any:
        if isinstance(v, list) and v:
            for b in v:
                if isinstance(b, dict) and len(b.get("text", "").strip()) < 5:
                    raise ValueError("Each block must contain actual script text")
        return v


class _DeleteSceneEffect(BaseModel):
    kind: Literal["delete_scene"] = "delete_scene"
    scene_num: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1)


class _ReorderScenesEffect(BaseModel):
    kind: Literal["reorder_scenes"] = "reorder_scenes"
    new_order: list[int] = Field(..., min_length=1)


class _QueueJobEffect(BaseModel):
    kind: Literal["queue_job"] = "queue_job"
    job_id: str
    job_type: Literal["tts", "ltx"]
    scene_num: int = Field(..., ge=1)
    block_id: str = Field(..., description="The block ID (e.g., 's1_b1' or 's2_b3'). Do NOT include the track prefix.")
    slot_id: str = Field(..., description="The canonical slot address in GSA (e.g., 'A1:1:s1_b1' or 'V1:2:s2_b3').")
    params: dict = Field(default_factory=dict)


class _JobStartedEffect(BaseModel):
    kind: Literal["job_started"] = "job_started"
    job_id: str
    vm_instance_id: str
    started_at: float = Field(default_factory=time.time)


class _JobCompletedEffect(BaseModel):
    kind: Literal["job_completed"] = "job_completed"
    job_id: str
    artifact_uri: str
    duration_sec: float = Field(..., ge=0.0)

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _parse_duration_sec(cls, val: Any) -> float:
        return parse_duration(val)
    vm_instance_id: str
    measurements: list[float] = Field(default_factory=list)


class _JobFailedEffect(BaseModel):
    kind: Literal["job_failed"] = "job_failed"
    job_id: str
    error_message: str
    failure_category: Literal["oom", "bad_prompt", "model_load_error", "disk_full", "network", "cuda_error", "unknown"]
    vm_instance_id: str
    retryable: bool = True
    retry_count: int = 0


class _JobRequeuedEffect(BaseModel):
    kind: Literal["job_requeued"] = "job_requeued"
    job_id: str
    reason: str = Field(..., min_length=1)
    new_params: dict | None = None


class _JobApprovedEffect(BaseModel):
    kind: Literal["job_approved"] = "job_approved"
    job_id: str
    artifact_uri: str
    quality_notes: str = ""
    reviewed_by: str = "agent"


class _AudioGeneratedEffect(BaseModel):
    kind: Literal["audio_generated"] = "audio_generated"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    artifact_uri: str


class _AudioMeasuredEffect(BaseModel):
    kind: Literal["audio_measured"] = "audio_measured"
    job_id: str
    block_id: str
    scene_num: int
    voice_role: str
    measured_sec: float

    @field_validator("measured_sec", mode="before")
    @classmethod
    def _parse_measured_sec(cls, val: Any) -> float:
        return parse_duration(val)
    measurements: list[float] = Field(default_factory=list)
    whisperx_confidence: float = 0.0


class _DurationAdjustedEffect(BaseModel):
    kind: Literal["duration_adjusted"] = "duration_adjusted"
    block_id: str = Field(..., description="The canonical slot address (e.g., 'A1:1:s1_b1').")
    slot_id: str = Field(..., description="The canonical slot address (e.g., 'A1:1:s1_b1').")
    scene_num: int
    voice_role: str
    scripted_sec: float
    measured_sec: float

    @field_validator("scripted_sec", "measured_sec", mode="before")
    @classmethod
    def _parse_secs(cls, val: Any) -> float:
        return parse_duration(val)


class _ReconciliationFailedEffect(BaseModel):
    kind: Literal["reconciliation_failed"] = "reconciliation_failed"
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    failures: list[ReconciliationFailureDetail] = Field(default_factory=list)
    worst_delta_sec: float
    suggested_adjustments: list[dict] = Field(default_factory=list)
    failure_type: Literal["duration_mismatch", "duration_unrecoverable"] = "duration_mismatch"


class _ReconciliationCompleteEffect(BaseModel):
    kind: Literal["reconciliation_complete"] = "reconciliation_complete"
    blocks_total: int
    blocks_passed: int
    blocks_failed: int
    worst_delta_sec: float
    total_measured_sec: float


class _VMAllocatedEffect(BaseModel):
    kind: Literal["vm_allocated"] = "vm_allocated"
    instance_id: str
    role: Literal["tts", "ltx"]
    offer_id: str
    worker_url: str
    gpu_type: str
    cost_per_hour: float


class _VMDeallocatedEffect(BaseModel):
    kind: Literal["vm_deallocated"] = "vm_deallocated"
    instance_id: str
    reason: Literal["job_done", "cost_limit", "stale", "provision_failed", "manual"]
    final_cost: float = 0.0
    runtime_sec: float = 0.0


class _VMProvisionFailedEffect(BaseModel):
    kind: Literal["vm_provision_failed"] = "vm_provision_failed"
    offer_id: str = ""
    job_id: str = ""
    error_message: str
    failure_category: Literal["no_offers", "offer_taken", "payment_failed", "boot_timeout", "ssh_failed", "unknown"]
    retryable: bool = True
    consecutive_failures: int = 1


class _VMObservedEffect(BaseModel):
    kind: Literal["vm_observed"] = "vm_observed"
    instance_id: str
    observed_status: Literal["running", "offline", "not_found", "unknown"]
    expected_status: Literal["running", "offline", "not_found", "unknown"]
    drift_description: str
    corrective_action: Literal["none", "escalate"] = "none"


class _MergeIntoOTIOEffect(BaseModel):
    kind: Literal["merge_into_otio"] = "merge_into_otio"
    job_id: str
    block_id: str = Field(..., description="The block ID (e.g., 's1_b1'). Do NOT include the track prefix.")
    scene_num: int
    slot_id: str = Field(..., description="The canonical slot address (e.g., 'A1:1:s1_b1' or 'V1:1:s1_b1').")
    artifact_uri: str
    track_name: Literal["A1_Narration", "V1_Video"]
    duration_sec: float
    transition_type: Literal["cut", "dissolve", "none"] = "cut"
    transition_duration_sec: float = 0.0

    @field_validator("duration_sec", "transition_duration_sec", mode="before")
    @classmethod
    def _parse_secs(cls, val: Any) -> float:
        return parse_duration(val)


class _DeleteFromOTIOEffect(BaseModel):
    kind: Literal["delete_from_otio"] = "delete_from_otio"
    job_id: str
    block_id: str = Field(..., description="The block ID (e.g., 's1_b1'). Do NOT include the track prefix.")
    scene_num: int
    slot_id: str = Field(..., description="The canonical slot address (e.g., 'A1:1:s1_b1').")
    track_name: Literal["A1_Narration", "V1_Video", "both"]
    reason: str


class _PipelineStartedEffect(BaseModel):
    kind: Literal["pipeline_started"] = "pipeline_started"
    config: dict = Field(default_factory=dict)
    max_tts_budget_usd: float = 2.0
    max_attempts_per_block: int = 5
    max_run_budget_usd: float = 10.0
    output_path: str = "/tmp/final_documentary.mp4"


class _PipelineCompleteEffect(BaseModel):
    kind: Literal["pipeline_complete"] = "pipeline_complete"
    output_path: str
    duration_sec: float

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _parse_duration_sec(cls, val: Any) -> float:
        return parse_duration(val)
    total_cost_usd: float = 0.0
    validation_passed: bool = True


class _PipelineAbortedEffect(BaseModel):
    kind: Literal["pipeline_aborted"] = "pipeline_aborted"
    reason: Literal["budget_exceeded", "vm_unavailable", "human_request", "loop_detected", "unknown"]
    error_log: list[str] = Field(default_factory=list)
    spent_usd: float = 0.0


class _VASTGlobalStateObservedEffect(BaseModel):
    kind: Literal["vast_global_state_observed"] = "vast_global_state_observed"
    credit_balance_usd: float = 0.0
    active_instance_count: int = 0
    current_billing_rate_usd_hr: float = 0.0
    observed_at: float = Field(default_factory=time.time)


class _BudgetSetEffect(BaseModel):
    kind: Literal["budget_set"] = "budget_set"
    budget_usd: float
    reason: str = "run_start"


class _BudgetExceededEffect(BaseModel):
    kind: Literal["budget_exceeded"] = "budget_exceeded"
    spent_usd: float
    limit_usd: float


class _HumanInstructionEffect(BaseModel):
    kind: Literal["human_instruction"] = "human_instruction"
    target_agent: str
    instruction: str
    from_human: str
    posted_at: float = Field(default_factory=time.time)
    priority: Literal["normal", "urgent", "blocking"] = "normal"
    action: Literal["budget_override", "emergency_abort", "approve_command", "revoke", "generic"] = "generic"
    action_params: dict = Field(default_factory=dict)


class _ClarificationRequestEffect(BaseModel):
    kind: Literal["clarification_request"] = "clarification_request"
    target_agent: str = "human"
    parser_category: str = ""
    raw_text: str = ""
    failure_reason: str = ""
    confidence_score: float = 0.0
    suggested_resolution: str = ""
    question: str = ""
    referenced_text: str = ""


class _AgentLoopDetectedEffect(BaseModel):
    kind: Literal["agent_loop_detected"] = "agent_loop_detected"
    agent: str
    loop_signature: str
    effect_sequence: list[str] = Field(default_factory=list)
    detection_mode: Literal["duplicate_effects", "alternating", "noop", "both"] = "both"
    detection_count: int = 1


class _ProductionFailedEffect(BaseModel):
    kind: Literal["production_failed"] = "production_failed"
    failure_type: Literal["overlap", "duration_mismatch", "gap_unexpected", "voice_mismatch", "visual_incoherence", "artistic_reject", "missing_media", "invalid_range", "track_misalignment", "audio_lufs"]
    slot_id: str = ""
    expected: str = ""
    actual: str = ""
    suggested_fix: SuggestedFix = Field(default_factory=SuggestedFix)
    vm_instance_id: str = ""
    attempt_number: int = 1


class _MeasurementRequestedEffect(BaseModel):
    kind: Literal["measurement_requested"] = "measurement_requested"
    job_id: str
    block_id: str
    scene_num: int
    slot_id: str


class _VideoMeasuredEffect(BaseModel):
    kind: Literal["video_measured"] = "video_measured"
    job_id: str
    block_id: str
    measured_sec: float

    @field_validator("measured_sec", mode="before")
    @classmethod
    def _parse_measured_sec(cls, val: Any) -> float:
        return parse_duration(val)


class _CommandExecutedEffect(BaseModel):
    kind: Literal["command_executed"] = "command_executed"
    command: str
    exit_code: int
    stdout_hash: str = ""


class _NetworkRequestEffect(BaseModel):
    kind: Literal["network_request"] = "network_request"
    url: str
    method: str
    status_code: int


class _FileWrittenEffect(BaseModel):
    kind: Literal["file_written"] = "file_written"
    filepath: str
    size_bytes: int


class _ProcessSpawnedEffect(BaseModel):
    kind: Literal["process_spawned"] = "process_spawned"
    target: str
    pid: int


_EffectUnion = Annotated[
    Union[
        _NoOpEffect,
        _UpdateScriptEffect,
        _DeleteSceneEffect,
        _ReorderScenesEffect,
        _QueueJobEffect,
        _JobStartedEffect,
        _JobCompletedEffect,
        _JobFailedEffect,
        _JobRequeuedEffect,
        _JobApprovedEffect,
        _AudioGeneratedEffect,
        _AudioMeasuredEffect,
        _DurationAdjustedEffect,
        _ReconciliationFailedEffect,
        _ReconciliationCompleteEffect,
        _VMAllocatedEffect,
        _VMDeallocatedEffect,
        _VMProvisionFailedEffect,
        _VMObservedEffect,
        _MergeIntoOTIOEffect,
        _DeleteFromOTIOEffect,
        _PipelineStartedEffect,
        _PipelineCompleteEffect,
        _PipelineAbortedEffect,
        _VASTGlobalStateObservedEffect,
        _BudgetSetEffect,
        _BudgetExceededEffect,
        _HumanInstructionEffect,
        _ClarificationRequestEffect,
        _AgentLoopDetectedEffect,
        _ProductionFailedEffect,
        _MeasurementRequestedEffect,
        _VideoMeasuredEffect,

        _CommandExecutedEffect,
        _NetworkRequestEffect,
        _FileWrittenEffect,
        _ProcessSpawnedEffect,
    ],
    Field(discriminator="kind"),
]


class _SingleEffect(BaseModel):
    """Agent parser schema: exactly one effect per turn."""
    chain_of_thought: str = Field(
        description="Step-by-step reasoning: what did you find? What effect? Why?"
    )
    effect: _EffectUnion = Field(
        description="The single extracted effect for this turn. NEVER hallucinate."
    )
    confidence: int = Field(ge=0, le=10, description="Confidence 0=empty, 10=perfect")

    @model_validator(mode="before")
    @classmethod
    def clean_fields(cls, data: Any) -> Any:
        def clean_val(val: Any) -> Any:
            if isinstance(val, str):
                return val.strip().replace("*", "").replace("`", "")
            elif isinstance(val, dict):
                return {k: clean_val(v) for k, v in val.items()}
            elif isinstance(val, list):
                return [clean_val(x) for x in val]
            return val
        return clean_val(data)


class _MultiEffect(BaseModel):
    """Batch/human parser schema: multiple effects allowed."""
    chain_of_thought: str = Field(
        description="Step-by-step reasoning: what did you find? What effects? Why?"
    )
    effects: list[_EffectUnion] = Field(
        description="List of extracted effects. Empty if no actionable data. NEVER hallucinate."
    )
    confidence: int = Field(ge=0, le=10, description="Confidence 0=empty, 10=perfect")

    @model_validator(mode="before")
    @classmethod
    def clean_fields(cls, data: Any) -> Any:
        def clean_val(val: Any) -> Any:
            if isinstance(val, str):
                return val.strip().replace("*", "").replace("`", "")
            elif isinstance(val, dict):
                return {k: clean_val(v) for k, v in val.items()}
            elif isinstance(val, list):
                return [clean_val(x) for x in val]
            return val
        return clean_val(data)


# ===========================================================================
# Parser API
# ===========================================================================

_SYSTEM_PROMPT = """\
You are an expert document parser for a documentary pipeline.

Your job: read free-form agent text and extract structured EFFECTS.

CRITICAL RULES:
1. Agents write in natural prose. Your job is to FIND the concrete data.
2. NEVER hallucinate. If the agent mentions an action but doesn't give actual data, DO NOT invent it.
3. Extract FULL CONTENT, not summaries. If the agent quotes narration, extract the complete quote.
4. If no actionable data exists, return noop.
5. Rate your confidence (0-10).

PERMITTED EFFECT KINDS:
{permitted_kinds}
"""

_DS_ASYNC_CLIENT: instructor.AsyncInstructor | None = None


def _ds_async_client() -> instructor.AsyncInstructor:
    global _DS_ASYNC_CLIENT
    if _DS_ASYNC_CLIENT is None:
        import os
        api_key = ""
        _deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
        if os.path.exists(_deepseek_key_path):
            with open(_deepseek_key_path) as f:
                api_key = f.read().strip()
        _DS_ASYNC_CLIENT = instructor.from_openai(
            AsyncOpenAI(
                api_key=api_key,
                base_url="https://api.deepseek.com/v1",
            ),
            mode=instructor.Mode.JSON,
        )
    return _DS_ASYNC_CLIENT


# Permitted kinds mapping per role
ROLE_PERMITTED_KINDS = {
    "scenario": ["update_script", "delete_scene", "reorder_scenes", "clarification_request", "noop"],
    "audio": ["queue_job", "job_approved", "job_requeued", "duration_adjusted", "reconciliation_failed", "reconciliation_complete", "clarification_request", "noop"],
    "video": ["queue_job", "job_approved", "job_requeued", "merge_into_otio", "clarification_request", "noop"],
    "assembly": ["pipeline_complete", "production_failed", "clarification_request", "noop"],
    "provisioner": ["vm_allocated", "vm_deallocated", "vm_provision_failed", "vm_observed", "job_completed", "job_failed", "job_started", "clarification_request", "noop"],
    "maintainer": ["human_instruction", "agent_loop_detected", "pipeline_aborted", "clarification_request", "noop"],
}


async def validate_state_invariants(agent_id: str, effect: Any) -> list[Effect]:
    try:
        import httpx
        from agent_base import get_gsa_url
        gsa_url = get_gsa_url()
        async with httpx.AsyncClient() as client:
            resp = await client.get(gsa_url)
            if resp.status_code == 200:
                gsa_state = resp.json()
            else:
                gsa_state = None
    except Exception:
        gsa_state = None

    if not gsa_state:
        return [effect]

    if effect.kind == "job_approved":
        jobs = gsa_state.get("jobs", {}).get("jobs", {})
        job = jobs.get(effect.job_id)
        if not job:
            return [
                ClarificationRequest(
                    agent=agent_id,
                    target_agent="human",
                    failure_reason=f"Job {effect.job_id} not found in GSA queue",
                    question=f"Agent attempted to approve job {effect.job_id}, but it does not exist in GSA."
                )
            ]
    elif effect.kind == "merge_into_otio":
        slots = gsa_state.get("otio", {}).get("slots", {})
        slot = slots.get(effect.slot_id)
        if not slot:
            return [
                ClarificationRequest(
                    agent=agent_id,
                    target_agent="human",
                    failure_reason=f"Slot {effect.slot_id} not found in GSA timeline",
                    question=f"Agent attempted to merge clip into slot {effect.slot_id}, but it is missing from timeline."
                )
            ]

    return [effect]


async def parse_agent_text_multi(agent_id: str, text: str) -> list[Effect]:
    """Extract effects from agent natural-language output.

    Uses _SingleEffect schema to enforce one-action-per-turn.
    Returns a list of length 0 or 1 for uniform interface.
    """
    permitted = ROLE_PERMITTED_KINDS.get(agent_id, ["noop", "clarification_request"])

    # Try deterministic parsing first (fast path for simulation/formatted calls)
    det_parsed = None
    try:
        import re
        import ast
        # Look for effect: <name>(<args>)
        match = re.search(r"effect:\s*([a-zA-Z0-9_]+)\s*\(", text)
        if match:
            func_name = match.group(1)
            start_idx = match.end()
            paren_count = 1
            end_idx = start_idx
            while end_idx < len(text) and paren_count > 0:
                char = text[end_idx]
                if char == '(':
                    paren_count += 1
                elif char == ')':
                    paren_count -= 1
                end_idx += 1
            if paren_count == 0:
                args_str = text[start_idx:end_idx-1].strip()
                # Parse keyword arguments using AST
                tree = ast.parse(f"f({args_str})")
                call_node = tree.body[0].value
                kwargs = {}
                for kw in call_node.keywords:
                    kwargs[kw.arg] = ast.literal_eval(kw.value)
                det_parsed = {"kind": func_name, **kwargs}
    except Exception as e:
        logger.debug(f"Deterministic parser failed: {e}")

    if det_parsed is not None:
        parsed_kind = det_parsed.get("kind")
        if parsed_kind not in permitted:
            logger.warning(f"Agent {agent_id} tried to emit non-permitted kind '{parsed_kind}', falling back to noop")
            return [NoOp(agent=agent_id, reason=f"Attempted non-permitted kind '{parsed_kind}'")]

        model_class = KIND_TO_MODEL.get(parsed_kind)
        if model_class is None:
            return [NoOp(agent=agent_id, reason=f"Unknown effect kind: {parsed_kind}")]

        # Construct the actual Effect subclass, injecting agent
        data = dict(det_parsed)
        data.pop("kind", None)
        data["agent"] = agent_id

        try:
            effect = model_class.model_validate(data)
            # Tier 3 validation: State Invariants
            return await validate_state_invariants(agent_id, effect)
        except Exception as e:
            exc = e
            return [
                ClarificationRequest(
                    agent=agent_id,
                    target_agent="human",
                    parser_category=agent_id,
                    raw_text=text,
                    failure_reason=f"Model validation failed: {exc}",
                    question=f"The parsed event model validation failed: {exc}. Raw agent text: '{text}'"
                )
            ]

    sys_prompt = _SYSTEM_PROMPT.format(permitted_kinds=", ".join(permitted))

    client = _ds_async_client()
    result = None
    parsed = None
    exc = None
    try:
        result = await client.chat.completions.create(
            model="deepseek-chat",
            response_model=_SingleEffect,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            max_retries=3,
        )
    except Exception as e:
        exc = e
        logger.error(f"Parser could not extract: {exc}")

    # Audit Logging
    try:
        from agent_base import get_active_log_dir
        parser_log_dir = get_active_log_dir()
        os.makedirs(parser_log_dir, exist_ok=True)
        with open(os.path.join(parser_log_dir, "parser_runs.log"), "a", encoding="utf-8") as f:
            f.write(f"\n\n--- PARSER RUN: {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            f.write(f"AGENT: {agent_id}\n")
            f.write(f"INPUT PROSE:\n{text}\n")
            if result:
                f.write(f"PARSED EVENT: {result.effect.kind}\n")
                f.write(f"CONFIDENCE: {result.confidence}\n")
            if exc:
                f.write(f"ERROR: {exc}\n")
    except Exception:
        pass  # Debug log write failures are non-critical and ignored

    if exc or result is None:
        err_msg = str(exc) if exc else "No result returned from extraction"
        return [
            ClarificationRequest(
                agent=agent_id,
                target_agent="human",
                parser_category=agent_id,
                raw_text=text,
                failure_reason=err_msg,
                question=f"An error occurred during semantic extraction: {err_msg}. Raw agent text: '{text}'"
            )
        ]

    # Tier 2 check: Low confidence
    if result.confidence < 7:
        return [
            ClarificationRequest(
                agent=agent_id,
                target_agent="human",
                parser_category=agent_id,
                raw_text=text,
                failure_reason=f"Confidence score {result.confidence} is below threshold",
                question=f"The parser could not extract a highly confident event from the text. Agent output: '{text}'"
            )
        ]

    parsed = result.effect
    if parsed.kind not in permitted:
        logger.warning(f"Agent {agent_id} tried to emit non-permitted kind '{parsed.kind}', falling back to noop")
        return [NoOp(agent=agent_id, reason=f"Attempted non-permitted kind '{parsed.kind}'")]

    model_class = KIND_TO_MODEL.get(parsed.kind)
    if model_class is None:
        return [NoOp(agent=agent_id, reason=f"Unknown effect kind: {parsed.kind}")]

    # Construct the actual Effect subclass, injecting agent
    data = parsed.model_dump()
    data["agent"] = agent_id

    try:
        effect = model_class.model_validate(data)
        # Tier 3 validation: State Invariants
        return await validate_state_invariants(agent_id, effect)
    except Exception as e:
        exc = e
        return [
            ClarificationRequest(
                agent=agent_id,
                target_agent="human",
                parser_category=agent_id,
                raw_text=text,
                failure_reason=f"Model validation failed: {exc}",
                question=f"The parsed event model validation failed: {exc}. Raw agent text: '{text}'"
            )
        ]


async def parse_human_text_multi(text: str) -> list[Effect]:
    """Extract multiple effects from human or batch input."""
    sys_prompt = _SYSTEM_PROMPT.format(permitted_kinds="all kinds allowed")
    client = _ds_async_client()
    try:
        result = await client.chat.completions.create(
            model="deepseek-chat",
            response_model=_MultiEffect,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            max_retries=3,
        )
    except Exception as exc:
        return [NoOp(agent="human", reason=f"Parser could not extract: {exc}")]

    effects: list[Effect] = []
    for parsed in result.effects:
        model_class = KIND_TO_MODEL.get(parsed.kind)
        if model_class is None:
            continue
        data = parsed.model_dump()
        data["agent"] = "human"
        try:
            effects.append(model_class.model_validate(data))
        except Exception:
            continue

    if not effects:
        return [NoOp(agent="human", reason="No actionable effects found")]

    return effects


def build_clarification_request(effects: list[Effect]) -> str | None:
    if len(effects) == 1 and effects[0].kind == "noop":
        reason = getattr(effects[0], "reason", "")
        if "missing" in reason.lower() or "placeholder" in reason.lower():
            return (
                "I understood you want to take action, but I couldn't find the "
                "concrete details in your response. Please include the actual values:\n"
                "- For audio: the exact text and voice (V1/V2/V3)\n"
                "- For video: the full visual description and duration\n"
                "- For provisioning: the offer ID and GPU type\n"
                "Write naturally — just make sure the specific data is in your prose."
            )
    return None
