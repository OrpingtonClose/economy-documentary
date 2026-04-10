"""
Assembler Agent -- final assembly of video + audio into the documentary.

Reads the OTIO timeline, trims video clips to match audio durations,
muxes audio and video for each scene, then concatenates all scenes
into the final documentary output.
"""

from __future__ import annotations

import logging

from google.adk.agents import Agent

from agents.model_config import build_model
from callbacks.timeline_guardian import timeline_guardian_callback
from tools.assembly_tools import concat_clips_tool, mux_audio_video_tool, trim_clip_tool
from tools.otio_tools import get_timeline_status_tool, validate_timeline_tool
from tools.video_tools import probe_clip_tool

logger = logging.getLogger(__name__)

_INSTRUCTION = """\
You are the Assembler Agent for a documentary pipeline.

Your job is to assemble the final documentary from the OTIO timeline.

WORKFLOW:
1. Call get_timeline_status() to see all clips on the timeline
2. For EACH scene (in order):
   a. Get the narration clips from A1_Narration
   b. Get the video clips from V1_Video
   c. Trim video clips to match narration duration:
      - Use trim_clip(input_path, start_sec=0, duration_sec=narration_duration, output_path)
      - The video was generated 15% longer than needed for this exact purpose
   d. Mux audio and video:
      - Use mux_audio_video(audio_path, video_path, output_path)
      - NO -shortest flag: video must be >= audio
3. Concatenate all muxed scene clips:
   - Use concat_clips(comma_separated_paths, output_path)
   - Output: /tmp/documentary-pipeline/output/final_documentary.mp4
4. Probe the final output to verify:
   - Use probe_clip(final_path) to check duration/resolution
5. Run final validation:
   - Call validate_timeline("assembly") to verify everything

RULES:
- Process scenes in order (scene 1 first, etc.)
- Video must ALWAYS be >= audio duration (no -shortest!)
- All subprocess calls use list form (no shell=True) — the tools handle this
- Output paths:
  - Trimmed: /tmp/documentary-pipeline/assembly/scene_NNN_trimmed.mp4
  - Muxed: /tmp/documentary-pipeline/assembly/scene_NNN_muxed.mp4
  - Final: /tmp/documentary-pipeline/output/final_documentary.mp4
"""


def _assembly_phase_setup(callback_context):
    """Set pipeline phase before assembler runs."""
    callback_context.state["pipeline_phase"] = "assembly"
    return None


assembler_agent = Agent(
    name="assembler_agent",
    model=build_model(),
    instruction=_INSTRUCTION,
    tools=[
        get_timeline_status_tool,
        validate_timeline_tool,
        trim_clip_tool,
        mux_audio_video_tool,
        concat_clips_tool,
        probe_clip_tool,
    ],
    before_agent_callback=_assembly_phase_setup,
    after_agent_callback=timeline_guardian_callback,
)
