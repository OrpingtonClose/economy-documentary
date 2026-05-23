"""Strongly-typed Pydantic models for LLM-structured outputs.

Replaces ``dict[str, Any]`` everywhere the pipeline parses JSON from LLM text.
"""

from __future__ import annotations

from .plan import FeaturePlan
from .scene import Scene, VisualStyle, VoiceLine
from .tool_result import (
    NarrationResult,
    OTIOClipResult,
    ToolCallOutcome,
    VideoRenderResult,
)
from .vm_state import VMRegistryDecision, VMState, WorkerStatus

__all__ = [
    "FeaturePlan",
    "NarrationResult",
    "OTIOClipResult",
    "Scene",
    "ToolCallOutcome",
    "VideoRenderResult",
    "VisualStyle",
    "VoiceLine",
    "VMRegistryDecision",
    "VMState",
    "WorkerStatus",
]
