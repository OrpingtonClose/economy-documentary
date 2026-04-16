"""Timing evaluator agent -- compares WhisperX timing against scenario intent.

Decides whether the audio timing is acceptable or needs refinement.
Outputs a structured TimingVerdict.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field
from strands import Agent

from agents.model_config import build_model
from plugins.concurrency_plugin import ConcurrencyPlugin
from plugins.dashboard_plugin import DashboardPlugin
from tools.environment_tools import query_production_capabilities, validate_plan
from tools.otio_tools import get_narration_durations_by_scene

logger = logging.getLogger(__name__)


class TimingViolation(BaseModel):
    """A single timing violation found during evaluation."""

    scene_num: int = Field(description="Scene number with the violation")
    voice: str = Field(description="Voice role (V1, V2, V3) if applicable")
    issue: str = Field(description="Description of the timing issue")
    actual_sec: float = Field(description="Actual duration in seconds")
    budget_sec: float = Field(description="Budget duration in seconds")


class TimingVerdict(BaseModel):
    """Structured output from the timing evaluator."""

    passed: bool = Field(description="Whether all timing checks passed")
    violations: list[TimingViolation] = Field(
        default_factory=list, description="List of timing violations found"
    )
    suggestions: list[str] = Field(
        default_factory=list, description="Suggestions for fixing violations"
    )


TIMING_EVALUATOR_PROMPT = """\
You are a timing evaluator. Compare actual TTS durations (from WhisperX alignment)
against the scenario's intended timing.

Check for:
- Scenes that exceed duration budgets
- Voice blocks that exceed video clip limits (use query_production_capabilities)
- Pacing issues (scenes too short or too long)
- Gaps between expected and actual durations

Use get_narration_durations_by_scene to read actual timing from the OTIO timeline.
Use query_production_capabilities to check video generation limits.
Use validate_plan to run gatekeeper checks.

Output a JSON verdict: {passed: bool, violations: [...], suggestions: [...]}
"""


def build_timing_evaluator() -> Agent:
    """Build and return the timing evaluator agent."""
    return Agent(
        name="timing_evaluator",
        system_prompt=TIMING_EVALUATOR_PROMPT,
        model=build_model(thinker=True),
        tools=[
            get_narration_durations_by_scene,
            query_production_capabilities,
            validate_plan,
        ],
        plugins=[ConcurrencyPlugin(), DashboardPlugin()],
    )
