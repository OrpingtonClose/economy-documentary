"""Audio Agent -- TTS generation + WhisperX alignment.

Generates TTS for all scenes (3 voices per scene) using Qwen3-TTS,
then runs WhisperX alignment on generated audio.
Writes alignment data to invocation_state["whisperx_alignment"].

The actual work is done deterministically via generate_all_narration tool
to avoid unreliable LLM tool-calling. Supports incremental re-generation
for scenes marked with '_changed': true.
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


@tool(context=True)
def generate_all_narration(tool_context=None) -> str:
    """Run deterministic TTS generation and WhisperX alignment for all scenes.

    Reads scenes from invocation_state, generates narration for each voice block,
    runs WhisperX alignment, and writes results back to invocation_state.
    Supports incremental re-generation: if a scene has '_changed': true,
    only that scene's audio is regenerated.

    Returns:
        Status summary of the narration generation.
    """
    from callbacks.deterministic_steps import deterministic_audio_callback

    # The deterministic callback expects a CallbackContext-like object.
    # We adapt the invocation_state to work with it.
    state = tool_context.invocation_state if tool_context else {}

    # Wrap state in StateDictProxy which delegates to the original dict by
    # reference (no copying) and adds .to_dict() for ADK compatibility.
    from callbacks._compat import StateDictProxy

    class _StateAdapter:
        def __init__(self, s: dict) -> None:
            self.state = s if isinstance(s, StateDictProxy) else StateDictProxy(s)

    adapter = _StateAdapter(state)

    try:
        result = deterministic_audio_callback(adapter)
        if result is not None:
            return f"Audio generation complete. Result: {result}"
        return "Audio generation complete."
    except RuntimeError:
        raise  # OTIO violations, contract failures are fatal
    except Exception as e:
        logger.exception("audio generation failed")
        return f"Audio generation failed: {e}"


_INSTRUCTION = """\
You are the Audio Agent for a documentary pipeline.

WORKFLOW:
1. Call validate_preconditions_tool("audio") to verify scenes exist in pipeline state.
   If preconditions fail, STOP and report the specific missing data — do NOT proceed.
2. Call generate_all_narration to run TTS generation and WhisperX alignment.
3. Call validate_otio_compliance to verify the OTIO timeline is structurally valid.
4. Call validate_deliverables("audio") to verify whisperx_alignment was produced
   and audio files exist on disk.

SELF-HEALING:
If validate_otio_compliance or validate_deliverables reports failures:
  a. Read the failure details — each error includes remediation hints
  b. For OTIO violations (gaps, drift): the audio durations may not match the
     timeline slots. Call generate_all_narration again to re-generate.
  c. For missing audio files: specific scenes may have failed TTS generation.
     Call generate_all_narration again — it supports incremental re-generation
     for scenes with missing audio.
  d. Re-validate after each fix attempt
  e. You may retry up to 3 times. If still failing, report ALL error details.
"""


def build_audio_agent() -> Agent:
    """Build and return the audio agent."""
    return Agent(
        name="audio_agent",
        system_prompt=_INSTRUCTION,
        model=build_model(),
        tools=[
            generate_all_narration,
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
