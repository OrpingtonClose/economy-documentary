"""Pydantic models for pipeline data that crosses agent boundaries via OTIO.

All data written to timeline.metadata['documentary'] is validated here
to prevent runtime serialization failures when json.dumps() is called.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class VoiceBlock(BaseModel):
    """A single voice block within a scene."""

    voice: str = Field(..., description="V1, V2, or V3")
    text: str = ""
    tone: str = ""


class PronunciationHints(BaseModel):
    """Pronunciation guidance for TTS."""

    model_config = {"extra": "allow"}


class Scene(BaseModel):
    """A single scene in the documentary scenario."""

    scene_num: int = Field(..., ge=0)
    title: str = ""
    duration_sec: float = Field(30.0, ge=1.0, le=120.0)
    voices: list[VoiceBlock] = Field(default_factory=list)
    visual_notes: str = ""
    dopamine_hook: str = ""
    pronunciation_hints: dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    narrative_purpose: str = ""
    visual_concept: str = ""
    hook_spec: dict[str, Any] = Field(default_factory=dict)
    outro_spec: dict[str, Any] = Field(default_factory=dict)

    @field_validator("voices", mode="before")
    @classmethod
    def _coerce_voices(cls, v: Any) -> list[dict]:
        if isinstance(v, list):
            return v
        return []

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _coerce_duration(cls, v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 30.0


class StyleLock(BaseModel):
    """The locked visual style family for the whole documentary."""

    dominant_style: str = ""
    forbidden_styles: list[str] = Field(default_factory=list)
    positive_fragment: str = ""
    negative_fragment: str = ""


class VisualStyle(BaseModel):
    """Visual style description for the documentary."""

    color_temperature: str = ""
    lighting: str = ""
    camera_movement: str = ""
    aspect_ratio: str = "16:9"
    grading_reference: str = ""
    texture_quality: str = ""
    mood: str = ""


class ScenarioDocument(BaseModel):
    """The full scenario output from the scenario stage."""

    scenes: list[Scene] = Field(default_factory=list)
    visual_style: VisualStyle = Field(default_factory=VisualStyle)
    style_lock: StyleLock = Field(default_factory=StyleLock)

    @field_validator("scenes", mode="before")
    @classmethod
    def _coerce_scenes(cls, v: Any) -> list[dict]:
        if isinstance(v, list):
            return v
        return []


def sanitize_for_json(value: Any) -> Any:
    """Recursively coerce non-JSON-serializable values to JSON-safe types.

    Used at the boundary before json.dumps() to prevent TypeError on
    datetime, Decimal, bytes, etc.
    """
    if isinstance(value, dict):
        return {str(k): sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_for_json(v) for v in value]
    if isinstance(value, BaseModel):
        return sanitize_for_json(value.model_dump(mode="json"))
    if isinstance(value, (int, float, str, bool, type(None))):
        return value
    return str(value)
