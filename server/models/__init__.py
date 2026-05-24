"""Strongly-typed Pydantic models for LLM-structured outputs.

Replaces ``dict[str, Any]`` everywhere the pipeline parses JSON from LLM text.
"""

from __future__ import annotations

from .plan import FeaturePlan
from .scene import Scene, VisualStyle, VoiceLine
from .job import Job, JobResult, JobStatus, JobType, QAResult
from .tool_result import (
    NarrationResult,
    OTIOClipResult,
    ToolCallOutcome,
    VideoRenderResult,
)
from .vm_state import VMRegistryDecision, VMState, WorkerStatus

__all__ = [
    "FeaturePlan",
    "Job",
    "JobResult",
    "JobStatus",
    "JobType",
    "NarrationResult",
    "OTIOClipResult",
    "QAResult",
    "Scene",
    "ToolCallOutcome",
    "VideoRenderResult",
    "VisualStyle",
    "VoiceLine",
    "VMRegistryDecision",
    "VMState",
    "WorkerStatus",
]
