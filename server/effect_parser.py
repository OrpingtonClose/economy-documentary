"""Parse agent text into typed algebraic effects.

Uses instructor + DeepSeek v4-flash with Chain-of-Thought prompting.
Agents write free text. The parser extracts structured effects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from effects import (
    Effect,
    GenerateNarrationAudio,
    JobCompleted,
    JobFailed,
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


class _SingleEffect(BaseModel):
    """One effect extracted from agent text.

    Examples:
        - "Narration V1: 'The rainbow arcs across the sky.'"
          → effect_type="UpdateScript", narration_v1="The rainbow arcs across the sky.", scene_num=1

        - "Create audio for V1: 'The rainbow arcs across the sky.'"
          → effect_type="GenerateNarrationAudio", voice="V1", text="The rainbow arcs across the sky.", scene_num=1

        - "Render: 'Wide shot of rainbow over mountains'"
          → effect_type="RenderVideoSegment", prompt="Wide shot of rainbow over mountains", scene_num=1

        - "Provisioned VM instance 12345 from offer 67890"
          → effect_type="VMAllocated", instance_id="12345", offer_id="67890"

        - "NoOp: waiting for media"
          → effect_type="NoOp", noop_reason="waiting for media"
    """

    effect_type: Literal[
        "UpdateScript", "GenerateNarrationAudio", "RenderVideoSegment",
        "MergeIntoOTIO", "VMAllocated", "VMDeallocated", "VMProvisionFailed",
        "JobStarted", "JobCompleted", "JobFailed",
        "JobQuestionReceived", "JobQuestionAnswered",
        "QAPassed", "QAFailed", "JobRequeued",
        "NoOp"
    ] = Field(description="The type of effect this represents")
    justification: str = Field(default="", description="Why this effect is being proposed")
    scene_num: int = Field(default=1, description="Scene number (default 1)")
    # UpdateScript fields
    narration_v1: str = Field(default="", description="Primary narration text - complete sentences")
    narration_v2: str = Field(default="", description="Alternative narration text")
    narration_v3: str = Field(default="", description="Third narration version")
    visual_notes: str = Field(default="", description="Visual description for video generation")
    dopamine_hook: str = Field(default="", description="Opening engagement hook")
    pronunciation_hints: str = Field(default="", description="Words needing special pronunciation")
    duration_sec: int = Field(default=30, description="Scene duration in seconds")
    # GenerateNarrationAudio fields
    voice: str = Field(default="", description="Voice identifier: V1, V2, or V3")
    text: str = Field(default="", description="Exact narration text to synthesize")
    # RenderVideoSegment fields
    prompt: str = Field(default="", description="Visual description for video generation")
    lora_id: str = Field(default="", description="LoRA model ID (usually empty)")
    # MergeIntoOTIO fields
    audio_clips: list[dict] = Field(default_factory=list)
    video_clips: list[dict] = Field(default_factory=list)
    # VM fields
    instance_id: str = Field(default="", description="VM instance ID")
    offer_id: str = Field(default="", description="Vast.ai offer ID")
    gpu_type: str = Field(default="", description="GPU type")
    worker_url: str = Field(default="", description="Worker URL if available")
    # VMProvisionFailed fields
    error_message: str = Field(default="", description="Why provisioning failed")
    # Job fields
    job_id: str = Field(default="", description="Job ID")
    worker_id: str = Field(default="", description="Worker that claimed the job")
    stage: str = Field(default="", description="audio or video")
    artifact_path: str = Field(default="", description="Path to generated file")
    # QA fields
    verdict: str = Field(default="", description="QA verdict text")
    comments: list[str] = Field(default_factory=list, description="Specific issues found")
    suggested_fix: str = Field(default="", description="How to fix the issues")
    # NoOp fields
    noop_reason: str = Field(default="", description="Why no action is taken")

    @field_validator("effect_type")
    @classmethod
    def validate_effect_type(cls, v: str) -> str:
        valid = {
            "UpdateScript", "GenerateNarrationAudio", "RenderVideoSegment",
            "MergeIntoOTIO", "VMAllocated", "VMDeallocated", "VMProvisionFailed",
            "JobStarted", "JobCompleted", "JobFailed",
            "JobQuestionReceived", "JobQuestionAnswered",
            "QAPassed", "QAFailed", "JobRequeued",
            "NoOp"
        }
        if v not in valid:
            raise ValueError(f"effect_type must be one of {valid}, got {v}")
        return v


class _MultiEffect(BaseModel):
    """Multiple effects extracted from agent text with reasoning.

    Examples:
        - Input: "V1: 'Rainbows are illusions.' V2: 'Light bends through water.'"
          → chain_of_thought="Found narration text for V1 and V2. This is a script update.",
            effects=[{effect_type:"UpdateScript", narration_v1:"Rainbows are illusions.", narration_v2:"Light bends through water."}],
            confidence=8

        - Input: "Can you give me a topic?"
          → chain_of_thought="Agent is asking a question, not proposing action.",
            effects=[{effect_type:"NoOp", noop_reason:"Agent asking for input"}],
            confidence=10

        - Input: "NoOp: waiting"
          → chain_of_thought="Agent explicitly says waiting.",
            effects=[{effect_type:"NoOp", noop_reason:"waiting"}],
            confidence=10
    """

    chain_of_thought: str = Field(
        description="Step-by-step reasoning: what did you find? What effects? Why?"
    )
    effects: list[_SingleEffect] = Field(
        description="List of extracted effects. Empty if no actionable data. NEVER hallucinate."
    )
    confidence: int = Field(
        ge=0, le=10,
        description="Confidence 0=empty chat, 10=perfectly clear effects"
    )

    @field_validator("effects")
    @classmethod
    def validate_effects(cls, v: list[_SingleEffect]) -> list[_SingleEffect]:
        # Ensure NoOp effects have a reason
        for effect in v:
            if effect.effect_type == "NoOp" and not effect.noop_reason:
                effect.noop_reason = "No action needed"
        return v


_SYSTEM_PROMPT = """You are an expert document parser for a documentary pipeline.

Your job: read free-form agent text and extract structured effects.

STEP-BY-STEP:
1. Read the ENTIRE text carefully.
2. Identify what the agent is trying to do.
3. Extract ALL structured data you can find — FULL text, not summaries.
4. If no actionable data exists, return empty effects list.
5. Rate your confidence (0-10).

EFFECT TYPES:
- UpdateScript: Script changes. Extract narration_v1, narration_v2, narration_v3, visual_notes, dopamine_hook, pronunciation_hints, duration_sec, scene_num.
  CRITICAL: Extract the FULL narration text, not a summary. If the text says "V1: 'The rainbow arcs across the sky.'", narration_v1="The rainbow arcs across the sky."
  Extract EVERYTHING between the label and the next section/header.

- GenerateNarrationAudio: TTS request. Extract voice, text, scene_num.
  CRITICAL: Extract the EXACT full narration text to synthesize.

- RenderVideoSegment: Video render. Extract prompt, lora_id, duration_sec, scene_num.
  CRITICAL: Extract the FULL visual description prompt.

- MergeIntoOTIO: Merge clips. Extract audio_clips, video_clips.
- VMAllocated: VM provisioned. Extract instance_id, offer_id, gpu_type, worker_url.
- VMDeallocated: VM destroyed. Extract instance_id, reason.
- VMProvisionFailed: Provisioning failed. Extract offer_id, error_message.
- JobStarted: Worker started job. Extract job_id, worker_id, stage.
- JobCompleted: Worker finished. Extract job_id, artifact_path, stage.
- JobFailed: Worker failed. Extract job_id, error_message, stage.
- QAPassed: QA approved. Extract job_id, artifact_path, verdict.
- QAFailed: QA rejected. Extract job_id, artifact_path, verdict, comments, suggested_fix.
- JobRequeued: Job sent back for retry. Extract job_id, comments, suggested_fix.
- NoOp: No action. Use ONLY if genuinely nothing found.

RULES:
- NEVER hallucinate. If text is just chatting, return empty effects.
- Extract FULL CONTENT, not labels or summaries. The text after "V1:" or "Narration:" is the content.
- One GenerateNarrationAudio per voice per scene.
- One RenderVideoSegment per scene.
- If text says "NoOp" or "waiting", return empty effects or single NoOp.
- Be conservative. Low confidence → fewer effects.
- If you see a full script with narration text, ALWAYS extract it into UpdateScript.
"""


def _build_effect(agent_id: str, text: str, parsed: _SingleEffect) -> Effect:
    """Construct a typed Effect from a parsed single effect."""
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

    cls = effect_classes.get(parsed.effect_type, NoOp)

    kwargs: dict[str, Any] = {
        "agent_id": agent_id,
        "timestamp": datetime.now(),
        "justification": parsed.justification or text,
        "scene_num": parsed.scene_num,
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




def _pre_extract_script(text: str) -> dict[str, str] | None:
    """Extract script fields using section-based parsing."""
    result: dict[str, str] = {}
    lines = text.splitlines()

    def _extract_section(header_keywords: list[str], stop_keywords: list[str]) -> str:
        """Extract text between a header and the next stop header."""
        capturing = False
        buffer: list[str] = []
        for line in lines:
            stripped = line.strip()
            # Check for stop keywords
            if capturing and any(kw.lower() in stripped.lower() for kw in stop_keywords):
                capturing = False
                break
            if capturing:
                # Strip blockquote markers
                if stripped.startswith(">"):
                    stripped = stripped[1:].strip()
                buffer.append(stripped)
            # Check for start keywords
            if any(kw.lower() in stripped.lower() for kw in header_keywords):
                capturing = True
        return "\n".join(buffer).strip()

    # V1
    v1 = _extract_section(
        ["V1", "Primary Narration", "Primary narration"],
        ["V2", "V3", "Alternate Narration", "Third Take", "Visual Notes", "---", "Duration Estimate"]
    )
    if v1:
        result["narration_v1"] = v1

    # V2
    v2 = _extract_section(
        ["V2", "Alternate Narration", "Alternate narration"],
        ["V3", "V1", "Primary Narration", "Third Take", "Visual Notes", "---", "Duration Estimate"]
    )
    if v2:
        result["narration_v2"] = v2

    # V3
    v3 = _extract_section(
        ["V3", "Third Take", "Third take"],
        ["V1", "V2", "Primary Narration", "Alternate Narration", "Visual Notes", "---", "Duration Estimate"]
    )
    if v3:
        result["narration_v3"] = v3

    # Visual notes
    visual = _extract_section(
        ["Visual Notes", "Visual notes", "Shot List", "frame-by-frame"],
        ["Duration Estimate", "Duration", "---", "Agent Note", "Pronunciation"]
    )
    if visual:
        result["visual_notes"] = visual

    # Dopamine hook
    hook = _extract_section(
        ["Dopamine Hook", "Dopamine hook", "Opening", "Hook"],
        ["Narration", "V1", "Visual Notes", "Pronunciation", "---", "Duration"]
    )
    if hook:
        result["dopamine_hook"] = hook

    if result.get("narration_v1"):
        return result
    return None


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

    Uses regex pre-extraction for common patterns, then instructor for validation.
    Never raises — on any failure returns [NoOp].
    """
    # Try regex pre-extraction for scripts first
    if agent_id == "scenario":
        pre = _pre_extract_script(text)
        if pre:
            print(f"  [PARSER] Regex pre-extract found v1_len={len(pre.get('narration_v1', ''))}")
            return [UpdateScript(
                agent_id=agent_id,
                timestamp=datetime.now(),
                justification=text,
                scene_num=1,
                narration_v1=pre.get("narration_v1", ""),
                narration_v2=pre.get("narration_v2", ""),
                narration_v3=pre.get("narration_v3", ""),
                visual_notes=pre.get("visual_notes", ""),
                dopamine_hook=pre.get("dopamine_hook", ""),
                duration_sec=30,
            )]

    # Fall back to instructor
    try:
        from structured_extract import extract

        parsed = extract(
            _MultiEffect,
            text,
            system_prompt=_SYSTEM_PROMPT,
            max_retries=3,
        )
    except Exception as exc:
        return [NoOp(
            agent_id=agent_id,
            timestamp=datetime.now(),
            justification=text,
            reason=f"Instructor parsing failed: {exc}",
        )]

    if not parsed.effects:
        return [NoOp(
            agent_id=agent_id,
            timestamp=datetime.now(),
            justification=text,
            reason="No actionable effects found",
        )]

    return [_build_effect(agent_id, text, p) for p in parsed.effects]
