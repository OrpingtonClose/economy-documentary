"""Algebraic effect types — the only things that can mutate the pipeline.

Every agent message is parsed into one of these effects.
Only validated effects are appended to the event store.
Only the projection handler applies effects to OTIO.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Effect(BaseModel):
    """Base class for all algebraic effects."""

    effect_type: str
    agent_id: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    justification: str = Field(default="", description="The raw text that caused this effect")
    scene_num: int = Field(default=0, description="Scene number this effect applies to")


class UpdateScript(Effect):
    """Scenario agent proposes script/narration/visual changes."""

    effect_type: Literal["UpdateScript"] = "UpdateScript"
    narration_v1: str = Field(default="", description="V1 Hook narration text")
    narration_v2: str = Field(default="", description="V2 Expert narration text")
    narration_v3: str = Field(default="", description="V3 Storyteller narration text")
    visual_notes: str = Field(default="", description="Visual description and shot notes")
    dopamine_hook: str = Field(default="", description="Dopamine hook phrase")
    pronunciation_hints: str = Field(default="", description="Pronunciation guidance")
    duration_sec: int = Field(default=30, description="Target scene duration in seconds")


class GenerateNarrationAudio(Effect):
    """Audio agent requests TTS generation for a voice line."""

    effect_type: Literal["GenerateNarrationAudio"] = "GenerateNarrationAudio"
    voice: str = Field(default="V1", description="Voice identifier: V1, V2, or V3")
    text: str = Field(default="", description="Narration text to synthesize")


class RenderVideoSegment(Effect):
    """Video agent requests LTX video generation for a scene."""

    effect_type: Literal["RenderVideoSegment"] = "RenderVideoSegment"
    prompt: str = Field(default="", description="LTX-2.3 video generation prompt")
    lora_id: str = Field(default="", description="LoRA identifier for style consistency")
    duration_sec: int = Field(default=5, description="Target clip duration in seconds")


class MergeIntoOTIO(Effect):
    """Assembly agent requests clips be merged into the OTIO timeline."""

    effect_type: Literal["MergeIntoOTIO"] = "MergeIntoOTIO"
    audio_clips: list[dict] = Field(default_factory=list, description="Audio clips to add to A1_Narration")
    video_clips: list[dict] = Field(default_factory=list, description="Video clips to add to V1_Video")


class ExecuteRawBash(Effect):
    """Any agent requests a bash command be executed.

    This is the escape hatch for operations not covered by other effect types.
    The agent must provide a clear reason for why bash is needed.
    """

    effect_type: Literal["ExecuteRawBash"] = "ExecuteRawBash"
    command: str = Field(default="", description="Bash command to execute")
    reason: str = Field(default="", description="Why bash is needed for this operation")


class NoOp(Effect):
    """No actionable effect detected. Either parsing failed or the message was informational."""

    effect_type: Literal["NoOp"] = "NoOp"
    reason: str = Field(default="", description="Why no effect was extracted")
