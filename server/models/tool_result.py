"""Pydantic models for tool call results — extracted from raw JSON text.

Every tool in the pipeline returns a JSON string. Currently the agent (or
callback code) does `json.loads(result)` and accesses dict keys. This is the
"types implied in text" boundary:

    Tool JSON string → extract(ToolResult) → typed object

The tool STILL returns raw text. The agent STILL reasons over text. But the
SYSTEM extracts structure before acting.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NarrationResult(BaseModel):
    """Result from generate_scene_narration tool."""

    status: str = Field(description="'generated', 'cached', or 'error'")
    output_path: str = Field(default="", description="Path to the WAV file")
    duration_sec: float = Field(default=0.0)
    voice: str = Field(default="")
    scene_num: int = Field(default=0)
    error: str = Field(default="")


class VideoRenderResult(BaseModel):
    """Result from submit_gpu_production_job / generate_video_clip tool."""

    status: str = Field(description="'rendered', 'queued', 'failed'")
    output_path: str = Field(default="", description="Path to MP4")
    duration_sec: float = Field(default=0.0)
    scene_num: int = Field(default=0)
    phrase_idx: int = Field(default=0)
    qa_quality: str = Field(default="", description="Quality score if available")
    qa_reason: str = Field(default="")
    error: str = Field(default="")


class OTIOClipResult(BaseModel):
    """Result from add_narration_to_timeline / add_video_clip_to_timeline."""

    status: str = Field(description="'added', 'already_exists', 'error'")
    track: str = Field(default="", description="'A1_Narration' or 'V1_Video'")
    scene_num: int = Field(default=0)
    phrase_idx: int = Field(default=0)
    clip_name: str = Field(default="")
    timeline_duration_after: float = Field(default=0.0)
    error: str = Field(default="")


class ToolCallOutcome(BaseModel):
    """Generic outcome wrapper for any tool call — extracted by the system."""

    tool_name: str = Field(description="Which tool was called")
    success: bool = Field(description="Did the tool claim success?")
    result_type: str = Field(
        default="",
        description="'narration', 'video', 'otio_clip', 'provisioning', 'other'",
    )
    extracted_result: BaseModel | None = Field(default=None)
    raw_output_preview: str = Field(default="", description="First 200 chars of raw output")
    error_message: str = Field(default="")
    suggested_action: str = Field(
        default="",
        description="'continue', 'retry', 'provision_worker', 'escalate'",
    )
