"""Strongly-typed Pydantic models for LLM-structured outputs.

Replaces ``dict[str, Any]`` everywhere the pipeline parses JSON from LLM text.
"""

from __future__ import annotations

from .plan import FeaturePlan
from .scene import Scene, VisualStyle, VoiceLine

__all__ = ['FeaturePlan', 'Scene', 'VisualStyle', 'VoiceLine', 'annotations']
