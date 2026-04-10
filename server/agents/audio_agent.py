"""
Audio Agent -- TTS generation + WhisperX alignment.

Generates TTS for all scenes (3 voices per scene) using Qwen3-TTS,
then runs WhisperX alignment on generated audio.
Writes alignment data to state["whisperx_alignment"].
"""

from __future__ import annotations

import logging

from google.adk.agents import Agent

from agents.model_config import build_model
from callbacks.timeline_guardian import timeline_guardian_callback
from tools.otio_tools import add_narration_clip_tool, create_timeline_tool, get_timeline_status_tool
from tools.tts_tools import generate_narration_tool
from tools.whisperx_tools import align_narration_tool

logger = logging.getLogger(__name__)

_INSTRUCTION = """\
You are the Audio Agent for a documentary pipeline.

Your job is to generate TTS narration, add clips to the OTIO timeline, and run
WhisperX alignment for every scene in the documentary.

Read the scenes from {scenes} (JSON array).  If the scenes string is wrapped
in markdown code fences (```json ... ```), strip them before parsing.

Language mode is "{language}".  Determine the WhisperX language code:
- "en"           → use "en"
- "ru"           → use "ru"
- "dual_ru_en"   → process EACH voice TWICE: once with lang="ru" for the
  [RU] text block and once with lang="en" for the [EN] text block.
  Use voice suffixes like V1_RU / V1_EN when adding narration clips.

For EACH scene, for EACH voice (V1, V2, V3), do ALL THREE steps:
1. Call generate_narration(scene_num, voice_role, text) → returns wav_path and duration.
2. Call add_narration_clip(scene_num, voice, wav_path, duration) → REQUIRED, adds
   the clip to the OTIO timeline. The pipeline WILL FAIL if you skip this step.
3. Call align_narration(wav_path, text, language_code) → returns word-level timestamps.

⚠️ CRITICAL: You MUST call add_narration_clip for EVERY WAV file you generate.
The Timeline Guardian validates that clips exist on the A1_Narration track.
If you skip add_narration_clip, the pipeline will fail with "A1_Narration track
not found" or "No narration clips found".

After processing ALL scenes:
- Compile all alignment data into a JSON dict keyed by "scene_NUM_VOICE"
  (e.g. "scene_001_V1_RU", "scene_001_V1_EN")
- Store the complete alignment data in state["whisperx_alignment"]

IMPORTANT:
- Process scenes in order (scene 1 first, then scene 2, etc.)
- Process all 3 voices for each scene before moving to the next scene
- Do NOT skip any scene or voice
- Do NOT skip add_narration_clip — it is mandatory for each generated WAV
- Use get_timeline_status() after all scenes to verify clips were added
"""


def _audio_phase_setup(callback_context):
    """Set pipeline phase before audio agent runs."""
    callback_context.state["pipeline_phase"] = "audio"
    return None


audio_agent = Agent(
    name="audio_agent",
    model=build_model(),
    instruction=_INSTRUCTION,
    tools=[
        generate_narration_tool,
        align_narration_tool,
        add_narration_clip_tool,
        create_timeline_tool,
        get_timeline_status_tool,
    ],
    output_key="whisperx_alignment",
    before_agent_callback=_audio_phase_setup,
    after_agent_callback=timeline_guardian_callback,
)
