"""
WhisperX Agent -- standalone WhisperX alignment agent + duration oracle.

Can be used as a sub-agent of audio_agent or standalone for re-alignment.
Runs WhisperX alignment on all generated TTS audio files and outputs
per-scene, per-word timing data.

WhisperX is the pipeline's duration oracle (#86).  The TTS engine
(Qwen3-TTS) reports inaccurate duration values — the PAG run produced
clips that were up to 44% shorter than claimed (scene 1: 35s claimed vs
19.8s actual = 56%).  Never trust TTS-reported duration; always measure
via this agent.

After alignment completes, :func:`whisperx_oracle_projection_callback`
builds a :class:`WhisperXOracle`, checks ``projected_total`` against
80 % of target, and fires a reflection event via
:func:`tools.otio_moments.fire_reflection_event` when the projection is
low.  That helper routes into ``supervisor_escalate`` from W3 so the
production supervisor can pick add_scenes / lengthen_scenes /
accept_runtime.
"""

from __future__ import annotations

import logging

from google.adk.agents import Agent

from agents.audio_agent import whisperx_oracle_callback
from agents.model_config import build_model
from tools.whisperx_tools import align_narration_tool

logger = logging.getLogger(__name__)

_INSTRUCTION = """\
You are the WhisperX Alignment Agent for a documentary pipeline.

Your job is to run WhisperX alignment on generated TTS audio files to produce
word-level timestamp data. This timing data tells the Visual Director when
content shifts happen in the narration.

WhisperX is the AUTHORITATIVE duration oracle for this pipeline. Never
trust the TTS engine's self-reported duration — always use WhisperX-
measured total_duration values. Any clip whose measurement is missing
or zero must be reported, not silently treated as "probably fine".

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
    # After the agent writes its alignment dict, run the oracle projection
    # so any runtime shortfall is detected + escalated BEFORE production
    # starts burning GPU time (#86).
    after_agent_callback=whisperx_oracle_callback,
)
