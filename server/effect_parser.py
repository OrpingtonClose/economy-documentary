"""Parse agent text into typed algebraic effects.

Every incoming raw text message from an agent is passed through instructor + Pydantic.
The parser extracts the agent's intention and materializes it as a typed Effect.

Failed parsing returns a safe NoOp — never crash.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from effects import (
    Effect,
    ExecuteRawBash,
    GenerateNarrationAudio,
    MergeIntoOTIO,
    NoOp,
    RenderVideoSegment,
    UpdateScript,
)


class _EffectDiscriminator(BaseModel):
    """Intermediate model for instructor parsing. Contains all possible fields."""

    effect_type: str = Field(description="One of: UpdateScript, GenerateNarrationAudio, RenderVideoSegment, MergeIntoOTIO, ExecuteRawBash, NoOp")
    justification: str = Field(default="", description="Why this effect is being proposed")
    scene_num: int = Field(default=0, description="Scene number")
    # UpdateScript fields
    narration_v1: str = Field(default="")
    narration_v2: str = Field(default="")
    narration_v3: str = Field(default="")
    visual_notes: str = Field(default="")
    dopamine_hook: str = Field(default="")
    pronunciation_hints: str = Field(default="")
    duration_sec: int = Field(default=30)
    # GenerateNarrationAudio fields
    voice: str = Field(default="")
    text: str = Field(default="")
    # RenderVideoSegment fields
    prompt: str = Field(default="")
    lora_id: str = Field(default="")
    # MergeIntoOTIO fields
    audio_clips: list[dict] = Field(default_factory=list)
    video_clips: list[dict] = Field(default_factory=list)
    # ExecuteRawBash fields
    command: str = Field(default="")
    reason: str = Field(default="")
    # NoOp fields
    noop_reason: str = Field(default="")


_SYSTEM_PROMPT = """You are an intent parser for a documentary pipeline.

Your job: read the agent's message and extract their intention as a structured effect.

Possible effect types:
- UpdateScript: The agent is proposing script changes (narration text, visual notes, timing)
- GenerateNarrationAudio: The agent wants text-to-speech for a specific voice
- RenderVideoSegment: The agent wants a video clip generated from a prompt
- MergeIntoOTIO: The agent wants clips merged into the timeline
- ExecuteRawBash: The agent wants a bash command executed
- NoOp: No actionable intent detected

Instructions:
1. Read the agent's message carefully.
2. Determine which effect type best matches their intent.
3. Extract all relevant fields.
4. If the intent is unclear, use NoOp.
5. Never guess — if uncertain, use NoOp.
"""


def parse_agent_text(agent_id: str, text: str) -> Effect:
    """Parse raw agent text into a typed Effect.

    Args:
        agent_id: The agent that produced this text (scenario, audio, video, etc.)
        text: The raw text message from the agent

    Returns:
        A typed Effect. Never raises — on any failure returns NoOp.
    """
    try:
        from structured_extract import extract

        parsed = extract(_EffectDiscriminator, text, system_prompt=_SYSTEM_PROMPT)
    except Exception as exc:
        return NoOp(
            agent_id=agent_id,
            timestamp=datetime.now(),
            justification=text,
            reason=f"Instructor parsing failed: {exc}",
        )

    effect_classes: dict[str, type[Effect]] = {
        "UpdateScript": UpdateScript,
        "GenerateNarrationAudio": GenerateNarrationAudio,
        "RenderVideoSegment": RenderVideoSegment,
        "MergeIntoOTIO": MergeIntoOTIO,
        "ExecuteRawBash": ExecuteRawBash,
        "NoOp": NoOp,
    }

    cls = effect_classes.get(parsed.effect_type, NoOp)

    # Build kwargs from parsed fields that exist in the target class
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
