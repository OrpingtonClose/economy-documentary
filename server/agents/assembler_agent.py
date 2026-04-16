"""Assembler Agent -- final assembly of video + audio into the documentary.

Reads the OTIO timeline, trims video clips to match audio durations,
muxes audio and video for each scene, then concatenates all scenes
into the final documentary output.

The actual work is done deterministically via render_final_video tool.
"""

from __future__ import annotations

import logging

from strands import Agent, tool

from agents.model_config import build_model
from plugins.concurrency_plugin import ConcurrencyPlugin
from plugins.dashboard_plugin import DashboardPlugin
from plugins.timeline_guardian_plugin import TimelineGuardianPlugin
from tools.validation_tools import (
    validate_deliverables,
    validate_otio_compliance,
    validate_preconditions_tool,
)

logger = logging.getLogger(__name__)


@tool
def render_final_video(tool_context=None) -> str:
    """Run deterministic assembly to produce the final documentary video.

    Reads the OTIO timeline from invocation_state, trims video clips to match
    audio durations, muxes audio and video for each scene, then concatenates
    all scenes into the final output.

    Returns:
        Status summary of the assembly.
    """
    from callbacks.deterministic_steps import deterministic_assembly_callback

    state = tool_context.invocation_state if tool_context else {}

    from callbacks._compat import StateDictProxy

    class _StateAdapter:
        def __init__(self, s: dict) -> None:
            self.state = s if isinstance(s, StateDictProxy) else StateDictProxy(s)

    adapter = _StateAdapter(state)

    try:
        result = deterministic_assembly_callback(adapter)
        if result is not None:
            return f"Assembly complete. Result: {result}"
        return "Assembly complete."
    except RuntimeError:
        raise  # OTIO violations are fatal
    except Exception as e:
        logger.exception("assembly failed")
        return f"Assembly failed: {e}"


_INSTRUCTION = """\
You are the Assembler Agent for a documentary pipeline.

WORKFLOW:
1. Call validate_preconditions_tool("assembly") to verify all inputs exist
   (scenes, whisperx_alignment, visual_concepts). If any are missing, STOP
   and report the specific missing data.
2. Call render_final_video to assemble the final documentary.
3. Call validate_otio_compliance to verify the final OTIO timeline.
4. Call validate_deliverables("assembly") to verify the output file exists.

SELF-HEALING:
If validate_otio_compliance or validate_deliverables reports failures:
  a. Read the failure details — each error includes remediation hints
  b. Assembly OTIO violations typically indicate upstream issues — report
     detailed errors so the system can escalate appropriately
  c. If the output file is missing or empty, retry render_final_video once
  d. You may retry up to 2 times. Report ALL errors if still failing.
"""


def build_assembler_agent() -> Agent:
    """Build and return the assembler agent."""
    return Agent(
        name="assembler_agent",
        system_prompt=_INSTRUCTION,
        model=build_model(),
        tools=[
            render_final_video,
            validate_deliverables,
            validate_otio_compliance,
            validate_preconditions_tool,
        ],
        plugins=[
            ConcurrencyPlugin(),
            DashboardPlugin(),
            TimelineGuardianPlugin(),
        ],
    )
