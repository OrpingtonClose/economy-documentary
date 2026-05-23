"""Pydantic models for scenario-generator output.

These replace the untyped ``list[dict[str, Any]]`` that currently flows
through ``state["scenes"]`` and ``_extract_scenes_array()``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class VoiceLine(BaseModel):
    """One narration line inside a scene."""

    voice: str = Field(description="Voice role identifier (e.g. narrator, host)")
    text: str = Field(description="Spoken text for TTS")
    tone: str = Field(default="neutral", description="Emotional tone hint")


class VisualStyle(BaseModel):
    """Global visual-style directive from the scenario generator."""

    style: str = Field(description="Overall visual aesthetic (e.g. cinematic noir)")
    avoid: list[str] = Field(
        default_factory=list,
        description="Visual tropes to explicitly exclude",
    )
    realism_anchors: list[str] = Field(
        default_factory=list,
        description="Concrete real-world references for grounding",
    )


class Scene(BaseModel):
    """One scene in the documentary scenario."""

    scene_num: int = Field(description="1-based scene index")
    narration_text: str = Field(
        default="",
        description="Flat narration text (legacy single-voice field)",
    )
    voices: list[VoiceLine] = Field(
        default_factory=list,
        description="Per-voice narration lines (preferred over narration_text)",
    )
    visual_description: str = Field(
        default="",
        description="Director-level visual brief for this scene",
    )
    duration_sec: float = Field(
        default=0.0,
        description="Target duration in seconds",
    )
    required_topics: list[str] = Field(
        default_factory=list,
        description="R0 topics that must appear in this scene",
    )
    forbidden_topics: list[str] = Field(
        default_factory=list,
        description="R0 topics that must NOT appear in this scene",
    )
