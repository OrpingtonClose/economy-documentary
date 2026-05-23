"""OTIO scene extraction — turn raw OTIO text into typed Pydantic models.

The pipeline's single source of truth is the OTIO file on disk. Agents read
text summaries of this file. This layer extracts typed Scene objects from
those text summaries using instructor + DeepSeek v4-flash.

Architecture:
    OTIO file → text summary → extract(Scene) → typed object → agent reasoning

This replaces the agent's implicit parsing of text summaries with explicit
structured extraction. The agent still sees text, but the system extracts
types from that text for:
- Checkpoint/resume (what scenes exist, which are complete)
- Recovery (detecting partial state mid-stage)
- Validation (ensuring scene structure is correct)
"""

from __future__ import annotations

import logging
from typing import Any

from models.scene import Scene
from structured_extract import extract

logger = logging.getLogger(__name__)


def extract_scenes_from_text(raw_text: str) -> list[Scene]:
    """Extract typed Scene objects from an OTIO text summary.

    The agent receives text like:
        "Timeline: documentary_draft
         A1_Narration (audio): 10 clips
           - s1_p0_narrator (4.2s)
           - s2_p0_narrator (3.8s)
         V1_Video (video): 8 clips
           - s1_v0 (5.1s)"

    This function extracts structured Scene objects from that text.
    """
    system = (
        "Extract structured scene data from an OTIO timeline text summary. "
        "Each scene has a scene_num, narration clips (with voice, text, duration), "
        "and video clips (with visual_description, duration). "
        "If the text does not contain scene information, return an empty list."
    )

    from pydantic import BaseModel, Field

    class SceneList(BaseModel):
        scenes: list[Scene] = Field(default_factory=list)

    result = extract(SceneList, raw_text, system_prompt=system, temperature=0.0)
    return result.scenes


def extract_scene_state(timeline_text: str) -> dict[str, Any]:
    """Extract overall pipeline state from an OTIO text summary.

    Returns:
        {
            "total_scenes": int,
            "audio_complete": bool,
            "video_complete": bool,
            "scenes_with_narration": list[int],
            "scenes_with_video": list[int],
            "scenes_complete": list[int],
            "scenes_missing_audio": list[int],
            "scenes_missing_video": list[int],
        }
    """
    from pydantic import BaseModel, Field

    class PipelineState(BaseModel):
        total_scenes: int = 0
        audio_complete: bool = False
        video_complete: bool = False
        scenes_with_narration: list[int] = Field(default_factory=list)
        scenes_with_video: list[int] = Field(default_factory=list)
        scenes_complete: list[int] = Field(default_factory=list)
        scenes_missing_audio: list[int] = Field(default_factory=list)
        scenes_missing_video: list[int] = Field(default_factory=list)

    system = (
        "Analyze an OTIO timeline text summary and determine the pipeline state. "
        "audio_complete means ALL scenes have narration clips. "
        "video_complete means ALL scenes have video clips. "
        "scenes_complete means BOTH audio and video are present for that scene."
    )

    result = extract(PipelineState, timeline_text, system_prompt=system, temperature=0.0)
    return result.model_dump(mode="json")


def summarize_scene_for_agent(scene: Scene) -> str:
    """Turn a typed Scene back into plain text for the agent.

    This is the reverse direction: typed object → text summary.
    The agent receives this text in its conversation.
    """
    lines = [
        f"Scene {scene.scene_num}:",
        f"  Visual: {scene.visual_description or '(not set)'}",
        f"  Duration target: {scene.duration_sec}s",
    ]

    if scene.voices:
        lines.append("  Narration:")
        for v in scene.voices:
            lines.append(f"    - [{v.voice}] {v.text[:80]}{'...' if len(v.text) > 80 else ''}")
    elif scene.narration_text:
        lines.append(f"  Narration: {scene.narration_text[:100]}{'...' if len(scene.narration_text) > 100 else ''}")
    else:
        lines.append("  Narration: (none)")

    if scene.required_topics:
        lines.append(f"  Required topics: {', '.join(scene.required_topics)}")
    if scene.forbidden_topics:
        lines.append(f"  Forbidden topics: {', '.join(scene.forbidden_topics)}")

    return "\n".join(lines)
