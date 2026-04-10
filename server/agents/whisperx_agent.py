"""
WhisperX Agent -- standalone WhisperX alignment agent.

Can be used as a sub-agent of audio_agent or standalone for re-alignment.
Runs WhisperX alignment on all generated TTS audio files and outputs
per-scene, per-word timing data.
"""

from __future__ import annotations

import logging

from google.adk.agents import Agent

from agents.model_config import build_model
from tools.whisperx_tools import align_narration_tool

logger = logging.getLogger(__name__)

_INSTRUCTION = """\
You are the WhisperX Alignment Agent for a documentary pipeline.

Your job is to run WhisperX alignment on generated TTS audio files to produce
word-level timestamp data. This timing data tells the Visual Director when
content shifts happen in the narration.

Read the scenes from {scenes} and any existing alignment from {whisperx_alignment}.

For each scene and voice that doesn't already have alignment data:
1. Determine the WAV file path (typically: /tmp/documentary-pipeline/audio/scene_NNN_VX.wav)
2. Extract the text from the scene's voice block
3. Call align_narration(wav_path, text, "en") to get word-level timestamps

Output: Complete alignment data as JSON dict keyed by "scene_NNN_VX", each containing
a "words" array of {{word, start, end}} objects.

Store the result in state["whisperx_alignment"].
"""

whisperx_agent = Agent(
    name="whisperx_agent",
    model=build_model(),
    instruction=_INSTRUCTION,
    tools=[align_narration_tool],
    output_key="whisperx_alignment",
)
