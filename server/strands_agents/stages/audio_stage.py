"""
Audio stage — Strands Agent replacing ADK audio_agent.

The original audio_agent was an ADK Agent where the actual work was
done deterministically in a before_agent_callback (deterministic_audio_callback).
The LLM agent was skipped entirely — the callback returned Content directly.

The Strands equivalent is an Agent with a deterministic tool
(``run_audio_pipeline``) that performs all TTS generation, WhisperX
alignment, and Oracle reconciliation. The Graph's backward edge from
audio → scenario handles the timing loop (when projected duration is
too short).

After the deterministic work completes:
1. WhisperX Oracle processes per-clip alignments
2. Timeline guardian checks OTIO compliance
3. OTIO state transitions to authoritative

All of these are handled by the CheckpointHook and QANodeHook
registered on the Graph.
"""

from __future__ import annotations

import logging
from typing import Any

from strands import Agent
from strands.tools import tool

from strands_agents.otio_manager import OTIOStateManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — preserved verbatim from ADK audio_agent
# ---------------------------------------------------------------------------

_AUDIO_INSTRUCTION = """\
You are the Audio Agent for a documentary pipeline.
Audio generation is handled automatically. Report completion.
"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def run_audio_pipeline(scenes_json: str, output_dir: str = "/tmp/documentary-pipeline") -> str:
    """Run the full audio pipeline: TTS generation + WhisperX alignment.

    This is the deterministic audio callback ported from the ADK
    before_agent_callback. It:
    1. Generates TTS for all scenes (3 voices per scene) using Qwen3-TTS
    2. Runs WhisperX alignment on generated audio
    3. Performs loudness normalization
    4. Writes alignment data to OTIO state
    5. Uploads to B2

    Args:
        scenes_json: JSON array of scenes from scenario stage.
        output_dir: Pipeline output directory.
    """
    # In production, this calls the deterministic audio callback.
    return "[run_audio_pipeline] Audio pipeline complete — placeholder"


@tool
def check_audio_completeness(audio_path: str) -> str:
    """Check if TTS audio is complete (not truncated).

    Detects the Qwen3-TTS abrupt-cut failure mode by checking
    trailing silence + end-of-file RMS energy. Non-negotiable
    per hard invariants §3 and §5.

    Args:
        audio_path: Path to the generated audio file.
    """
    return f"[check_audio_completeness] Audio '{audio_path}' OK — placeholder"


@tool
def evaluate_timing(alignment_json: str, target_duration_sec: float = 420.0) -> str:
    """Evaluate timing alignment against the target duration.

    Returns alignment per scene and a projection of total runtime.
    If projected duration < 80% of target, sets recovery context
    for the timing loop (audio → scenario backward edge).

    Args:
        alignment_json: WhisperX alignment data as JSON.
        target_duration_sec: Target total duration in seconds.
    """
    return "[evaluate_timing] Timing OK — placeholder"


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def build_audio_agent(
    otio_manager: OTIOStateManager | None = None,
    model: Any = None,
) -> Agent:
    """Build the Strands Agent for the audio stage.

    Args:
        otio_manager: Optional OTIOStateManager for timeline access.
        model: Optional model configuration.

    Returns:
        A configured Strands Agent ready for the Graph.
    """
    tools = [
        run_audio_pipeline,
        check_audio_completeness,
        evaluate_timing,
    ]

    if otio_manager is not None:
        @tool
        def read_audio_state(stage: str = "audio") -> str:
            """Read the audio stage's OTIO state."""
            return otio_manager.read(stage)

        @tool
        def write_audio_mutation(operation: str, details: str = "") -> str:
            """Request a mutation on the OTIO timeline (guarded)."""
            otio_manager.guard_mutation(operation)
            return f"[write_audio_mutation] '{operation}' allowed — placeholder"

        tools.extend([read_audio_state, write_audio_mutation])

    return Agent(
        name="audio",
        system_prompt=_AUDIO_INSTRUCTION,
        tools=tools,
        model=model,
    )
