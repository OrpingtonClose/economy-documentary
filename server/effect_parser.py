"""Semantic parser — extracts typed algebraic effects from agent natural language.

Agents write free-form prose and nothing else. They do not emit markers, JSON,
section labels, or any structured format. ALL extraction complexity lives here
in the parser. The parser uses instructor + deepseek-v4-flash with strict
discriminated-union validation. Complexity is expected and welcome.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, field_validator
from typing_extensions import Annotated

from effects import (
    Effect,
    GenerateNarrationAudio,
    JobCompleted,
    JobFailed,
    JobQuestionAnswered,
    JobQuestionReceived,
    JobRequeued,
    JobStarted,
    MergeIntoOTIO,
    NoOp,
    QAFailed,
    QAPassed,
    RenderVideoSegment,
    UpdateScript,
    VMAllocated,
    VMDeallocated,
    VMProvisionFailed,
)


# ---------------------------------------------------------------------------
# Per-effect-type strict models — REQUIRED fields trigger instructor reasking
# ---------------------------------------------------------------------------

class _NoOpEffect(BaseModel):
    effect_type: Literal["NoOp"]
    noop_reason: str = ""


class _UpdateScriptEffect(BaseModel):
    effect_type: Literal["UpdateScript"]
    narration_v1: str
    narration_v2: str = ""
    narration_v3: str = ""
    visual_notes: str = ""
    dopamine_hook: str = ""
    pronunciation_hints: str = ""
    duration_sec: int = 30
    scene_num: int = 1

    @field_validator("narration_v1", mode="before")
    @classmethod
    def _v1_must_be_real(cls, v: Any) -> Any:
        if isinstance(v, str) and len(v.strip()) < 5:
            raise ValueError("narration_v1 must contain actual script text, not a placeholder")
        return v


class _GenerateNarrationAudioEffect(BaseModel):
    effect_type: Literal["GenerateNarrationAudio"]
    voice: str
    text: str
    scene_num: int = 1

    @field_validator("voice", mode="before")
    @classmethod
    def _voice_must_be_v123(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip() not in ("V1", "V2", "V3"):
            raise ValueError(f"voice must be V1, V2, or V3 (got '{v}')")
        return v

    @field_validator("text", mode="before")
    @classmethod
    def _text_must_be_real(cls, v: Any) -> Any:
        if not isinstance(v, str) or len(v.strip()) < 3:
            raise ValueError("text must be the actual narration text to synthesize")
        return v


class _RenderVideoSegmentEffect(BaseModel):
    effect_type: Literal["RenderVideoSegment"]
    prompt: str
    duration_sec: int = 5
    scene_num: int = 1

    @field_validator("prompt", mode="before")
    @classmethod
    def _prompt_must_be_real(cls, v: Any) -> Any:
        if not isinstance(v, str) or len(v.strip()) < 5:
            raise ValueError("prompt must be a real visual description, not a placeholder")
        return v


class _VMAllocatedEffect(BaseModel):
    effect_type: Literal["VMAllocated"]
    offer_id: str
    gpu_type: str
    worker_url: str = ""

    @field_validator("offer_id", mode="before")
    @classmethod
    def _offer_must_be_numeric(cls, v: Any) -> Any:
        if not isinstance(v, str) or not v.strip().isdigit():
            raise ValueError(f"offer_id must be a numeric Vast.ai offer ID (got '{v}')")
        return v

    @field_validator("gpu_type", mode="before")
    @classmethod
    def _gpu_must_be_real(cls, v: Any) -> Any:
        if not isinstance(v, str) or len(v.strip()) < 2:
            raise ValueError(f"gpu_type must name a real GPU (got '{v}')")
        return v


class _VMDeallocatedEffect(BaseModel):
    effect_type: Literal["VMDeallocated"]
    instance_id: str
    reason: str = ""

    @field_validator("instance_id", mode="before")
    @classmethod
    def _id_must_be_numeric(cls, v: Any) -> Any:
        if not isinstance(v, str) or not v.strip().isdigit():
            raise ValueError(f"instance_id must be a numeric VM ID (got '{v}')")
        return v


class _VMProvisionFailedEffect(BaseModel):
    effect_type: Literal["VMProvisionFailed"]
    offer_id: str = ""
    error_message: str = ""


class _MergeIntoOTIOEffect(BaseModel):
    effect_type: Literal["MergeIntoOTIO"]
    audio_clips: list[dict] = Field(default_factory=list)
    video_clips: list[dict] = Field(default_factory=list)


class _JobStartedEffect(BaseModel):
    effect_type: Literal["JobStarted"]
    job_id: str = ""
    worker_id: str = ""
    stage: str = ""


class _JobCompletedEffect(BaseModel):
    effect_type: Literal["JobCompleted"]
    job_id: str = ""
    artifact_path: str = ""
    stage: str = ""


class _JobFailedEffect(BaseModel):
    effect_type: Literal["JobFailed"]
    job_id: str = ""
    error_message: str = ""
    stage: str = ""


class _JobQuestionReceivedEffect(BaseModel):
    effect_type: Literal["JobQuestionReceived"]
    job_id: str = ""
    question: str = ""


class _JobQuestionAnsweredEffect(BaseModel):
    effect_type: Literal["JobQuestionAnswered"]
    job_id: str = ""
    answer: str = ""


class _QAPassedEffect(BaseModel):
    effect_type: Literal["QAPassed"]
    job_id: str = ""
    artifact_path: str = ""
    verdict: str = ""


class _QAFailedEffect(BaseModel):
    effect_type: Literal["QAFailed"]
    job_id: str = ""
    artifact_path: str = ""
    verdict: str = ""
    comments: list[str] = Field(default_factory=list)
    suggested_fix: str = ""


class _JobRequeuedEffect(BaseModel):
    effect_type: Literal["JobRequeued"]
    job_id: str = ""
    comments: list[str] = Field(default_factory=list)
    suggested_fix: str = ""


_EffectUnion = Annotated[
    Union[
        _NoOpEffect,
        _UpdateScriptEffect,
        _GenerateNarrationAudioEffect,
        _RenderVideoSegmentEffect,
        _VMAllocatedEffect,
        _VMDeallocatedEffect,
        _VMProvisionFailedEffect,
        _MergeIntoOTIOEffect,
        _JobStartedEffect,
        _JobCompletedEffect,
        _JobFailedEffect,
        _JobQuestionReceivedEffect,
        _JobQuestionAnsweredEffect,
        _QAPassedEffect,
        _QAFailedEffect,
        _JobRequeuedEffect,
    ],
    Field(discriminator="effect_type"),
]


class _MultiEffect(BaseModel):
    """Multiple effects extracted from agent text with reasoning."""

    chain_of_thought: str = Field(
        description="Step-by-step reasoning: what did you find? What effects? Why?"
    )
    effects: list[_EffectUnion] = Field(
        description="List of extracted effects. Empty if no actionable data. NEVER hallucinate."
    )
    confidence: int = Field(
        ge=0, le=10,
        description="Confidence 0=empty chat, 10=perfectly clear effects"
    )


# ---------------------------------------------------------------------------
# System prompt — all extraction complexity lives here
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an expert semantic parser for a documentary pipeline.

Agents write NATURAL LANGUAGE ONLY. They do not use markers, JSON, labels, or any
structured format. Your job is to READ their free-form prose and EXTRACT structured
effects using deep semantic understanding.

CRITICAL RULES:
1. Agents write in natural prose. FIND the concrete data hidden in their text.
2. NEVER hallucinate. If the agent mentions an action but doesn't give actual data, DO NOT invent it.
3. Extract FULL CONTENT, not summaries. If the agent quotes narration, extract the complete quote.
4. If no actionable data exists, return an empty effects list.
5. Rate your confidence (0-10).

EFFECT TYPES:
- UpdateScript: Script changes. Extract narration_v1, narration_v2, narration_v3, visual_notes, dopamine_hook, pronunciation_hints, duration_sec, scene_num.
  The agent's prose may contain narration like: "V1 says 'The rainbow arcs...'" or "Primary narration: The rainbow arcs..." — extract the FULL text.

- GenerateNarrationAudio: TTS request. Extract voice (V1/V2/V3) and text (exact narration).
  The agent may say: "For V1, use: 'In ADHD...'" — extract voice=V1, text="In ADHD..."

- RenderVideoSegment: Video render. Extract prompt (full visual description) and duration_sec.
  The agent may describe a scene in detail — that description IS the prompt.

- VMAllocated: VM provisioned. Extract offer_id (numeric ID) and gpu_type (e.g., RTX_4090, H100).
  The agent may mention: "offer 12345 with an RTX 4090" — extract offer_id="12345", gpu_type="RTX_4090".

- VMDeallocated: VM destroyed. Extract instance_id (numeric ID) and reason.

- NoOp: No action. Use ONLY if genuinely nothing found.

If an agent says they will do something but doesn't provide the concrete details, do NOT return that effect. Return empty effects instead — the pipeline will ask the agent for clarification.
"""


# ---------------------------------------------------------------------------
# Effect construction
# ---------------------------------------------------------------------------

def _build_effect(agent_id: str, text: str, parsed: Any) -> Effect:
    """Construct a typed Effect from a parsed union member."""
    effect_classes: dict[str, type[Effect]] = {
        "UpdateScript": UpdateScript,
        "GenerateNarrationAudio": GenerateNarrationAudio,
        "RenderVideoSegment": RenderVideoSegment,
        "MergeIntoOTIO": MergeIntoOTIO,
        "VMAllocated": VMAllocated,
        "VMDeallocated": VMDeallocated,
        "VMProvisionFailed": VMProvisionFailed,
        "JobStarted": JobStarted,
        "JobCompleted": JobCompleted,
        "JobFailed": JobFailed,
        "JobQuestionReceived": JobQuestionReceived,
        "JobQuestionAnswered": JobQuestionAnswered,
        "QAPassed": QAPassed,
        "QAFailed": QAFailed,
        "JobRequeued": JobRequeued,
        "NoOp": NoOp,
    }

    et = parsed.effect_type
    cls = effect_classes.get(et, NoOp)

    kwargs: dict[str, Any] = {
        "agent_id": agent_id,
        "timestamp": datetime.now(),
        "justification": text,
        "scene_num": getattr(parsed, "scene_num", 1),
    }

    for field_name in cls.model_fields:
        if field_name in ("effect_type", "agent_id", "timestamp", "justification", "scene_num"):
            continue
        if hasattr(parsed, field_name):
            kwargs[field_name] = getattr(parsed, field_name)

    try:
        return cls(**kwargs)
    except Exception as exc:
        return NoOp(
            agent_id=agent_id,
            timestamp=datetime.now(),
            justification=text,
            reason=f"Effect construction failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Post-parse clarification
# ---------------------------------------------------------------------------

def build_clarification_request(effects: list[Effect]) -> str | None:
    """If effects were parsed but are NoOp due to missing data, ask the agent.

    Called AFTER instructor extraction + validation. If the parser
    couldn't find concrete data, we ask the agent directly.
    """
    if len(effects) == 1 and effects[0].effect_type == "NoOp":
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


# ---------------------------------------------------------------------------
# Main parse entry points
# ---------------------------------------------------------------------------

def parse_agent_text(agent_id: str, text: str) -> Effect:
    """Parse raw agent text into a single typed Effect."""
    effects = parse_agent_text_multi(agent_id, text)
    for e in effects:
        if e.effect_type != "NoOp":
            return e
    return effects[0] if effects else NoOp(
        agent_id=agent_id,
        timestamp=datetime.now(),
        justification=text,
        reason="No effects extracted",
    )


def parse_agent_text_multi(agent_id: str, text: str) -> list[Effect]:
    """Parse raw agent text into a list of typed Effects.

    Single-phase semantic extraction via instructor + deepseek-v4-flash.
    All complexity lives in the parser. Agents write natural language only.
    """
    try:
        from structured_extract import extract

        parsed = extract(
            _MultiEffect,
            text,
            system_prompt=_SYSTEM_PROMPT,
            max_retries=3,
        )
    except Exception as exc:
        # Instructor exhausted retries — effect genuinely couldn't be extracted
        return [NoOp(
            agent_id=agent_id,
            timestamp=datetime.now(),
            justification=text,
            reason=f"Parser could not extract effects: {exc}",
        )]

    if not parsed.effects:
        return [NoOp(
            agent_id=agent_id,
            timestamp=datetime.now(),
            justification=text,
            reason="No actionable effects found in agent text",
        )]

    return [_build_effect(agent_id, text, p) for p in parsed.effects]
