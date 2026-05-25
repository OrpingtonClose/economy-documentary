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
    ExecuteRawBash,
    GenerateNarrationAudio,
    MergeIntoOTIO,
    NoOp,
    RenderVideoSegment,
    UpdateScript,
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

        - "NoOp: waiting for media"
          → effect_type="NoOp", noop_reason="waiting for media"
    """

    effect_type: Literal[
        "UpdateScript", "GenerateNarrationAudio", "RenderVideoSegment",
        "MergeIntoOTIO", "ExecuteRawBash", "NoOp"
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
    # ExecuteRawBash fields
    command: str = Field(default="", description="Bash command to execute")
    reason: str = Field(default="", description="Why the command is needed")
    # NoOp fields
    noop_reason: str = Field(default="", description="Why no action is taken")

    @field_validator("effect_type")
    @classmethod
    def validate_effect_type(cls, v: str) -> str:
        valid = {"UpdateScript", "GenerateNarrationAudio", "RenderVideoSegment",
                "MergeIntoOTIO", "ExecuteRawBash", "NoOp"}
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
1. Read the entire text carefully.
2. Identify what the agent is trying to do.
3. Extract ALL structured data you can find.
4. If no actionable data exists, return empty effects list.
5. Rate your confidence (0-10).

EFFECT TYPES:
- UpdateScript: Script changes. Extract narration_v1, narration_v2, narration_v3, visual_notes, dopamine_hook, pronunciation_hints, duration_sec, scene_num.
- GenerateNarrationAudio: TTS request. Extract voice, text, scene_num.
- RenderVideoSegment: Video render. Extract prompt, lora_id, duration_sec, scene_num.
- MergeIntoOTIO: Merge clips. Extract audio_clips, video_clips.
- ExecuteRawBash: Bash command. Extract command, reason.
- NoOp: No action. Use ONLY if genuinely nothing found.

RULES:
- NEVER hallucinate. If text is just chatting, return empty effects.
- Extract actual content, not labels. "Narration V1" is a label; the quoted text after it is the content.
- One GenerateNarrationAudio per voice per scene.
- One RenderVideoSegment per scene.
- If text says "NoOp" or "waiting", return empty effects or single NoOp.
- Be conservative. Low confidence → fewer effects.
"""


def _build_effect(agent_id: str, text: str, parsed: _SingleEffect) -> Effect:
    """Construct a typed Effect from a parsed single effect."""
    effect_classes: dict[str, type[Effect]] = {
        "UpdateScript": UpdateScript,
        "GenerateNarrationAudio": GenerateNarrationAudio,
        "RenderVideoSegment": RenderVideoSegment,
        "MergeIntoOTIO": MergeIntoOTIO,
        "ExecuteRawBash": ExecuteRawBash,
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

    Uses instructor with Chain-of-Thought prompting and reask validation.
    Never raises — on any failure returns [NoOp].
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
