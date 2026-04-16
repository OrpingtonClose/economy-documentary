"""WhisperX Agent -- standalone WhisperX alignment agent.

Can be used as a sub-agent of audio_agent or standalone for re-alignment.
Runs WhisperX alignment on all generated TTS audio files and outputs
per-scene, per-word timing data.
"""

from __future__ import annotations

import logging

from strands import Agent

from agents.model_config import build_model
from tools.whisperx_tools import align_narration

logger = logging.getLogger(__name__)

_INSTRUCTION = """\
You are the WhisperX Alignment Agent for a documentary pipeline.

Your job is to run WhisperX alignment on generated TTS audio files to produce
word-level timestamp data. This timing data tells the Visual Director when
content shifts happen in the narration.

For each scene and voice that doesn't already have alignment data:
1. Determine the WAV file path (typically: /tmp/documentary-pipeline/audio/scene_NNN_VX.wav)
2. Extract the text from the scene's voice block
3. Call align_narration(wav_path, text, "en") to get word-level timestamps

Output: Complete alignment data as JSON dict keyed by "scene_NNN_VX", each containing
a "words" array of {word, start, end} objects.
"""


def build_whisperx_agent() -> Agent:
    """Build and return the WhisperX alignment agent."""
    return Agent(
        name="whisperx_agent",
        system_prompt=_INSTRUCTION,
        model=build_model(),
        tools=[align_narration],
    )
