"""
Production Supervisor -- orchestrates actual video generation on GPU.

Reads visual concepts from state["visual_concepts"], provisions GPU VMs
via Vast.ai, generates video clips using LTX-2.3, probes results, and
adds clips to the OTIO timeline.

Uses the escalate pattern from MiroThinker's thinker: monitors progress
and signals completion when all clips are generated and validated.
"""

from __future__ import annotations

import logging

from google.adk.agents import Agent

from agents.model_config import build_model
from callbacks.timeline_guardian import timeline_guardian_callback
from tools.otio_tools import (
    add_video_clip_tool,
    get_timeline_status_tool,
    validate_timeline_tool,
)
from tools.vastai_tools import (
    check_vm_status_tool,
    list_active_vms_tool,
    provision_gpu_vm_tool,
    terminate_vm_tool,
)
from tools.video_tools import generate_video_clip_tool, probe_clip_tool

logger = logging.getLogger(__name__)

_INSTRUCTION = """\
You are the Production Supervisor for a documentary pipeline.

Your job is to generate all video clips from the visual concepts and add them
to the OTIO timeline.

Read the visual concepts from {visual_concepts}.

WORKFLOW:
1. Check available GPU VMs with list_active_vms()
2. If no VMs available, provision one with provision_gpu_vm()
3. Wait for VM to be ready with check_vm_status()
4. For EACH visual concept (in scene order):
   a. Generate the video clip:
      - Use generate_video_clip(prompt, duration_sec, lora_id, lora_weight, output_path)
      - Duration MUST be target_duration * 1.15 (15% margin for trim)
      - bf16 only, no FP8, no quantization
   b. Probe the generated clip:
      - Use probe_clip(mp4_path) to verify duration/resolution
      - Check that actual duration >= target duration
   c. Add to OTIO timeline:
      - Use add_video_clip(scene_num, phrase_idx, mp4_path, duration,
        source_range, available_range, lora_id)
      - source_range = target audio duration
      - available_range = actual video duration (from probe)

5. After all clips are generated:
   - Call get_timeline_status() to verify all clips are in place
   - Call validate_timeline("production") to run production validation
   - If validation passes, you're done

6. Terminate GPU VMs when finished:
   - Use terminate_vm(vm_id) for each provisioned VM
   - ALWAYS clean up VMs to avoid charges

RULES:
- Process scenes in order
- Never create duplicate clips (the tools handle idempotency)
- If a clip generation fails, retry once before reporting error
- Video must always be >= audio duration
- Output paths: /tmp/documentary-pipeline/video/scene_NNN_phrase_NNN.mp4
"""


def _production_phase_setup(callback_context):
    """Set pipeline phase before production supervisor runs."""
    callback_context.state["pipeline_phase"] = "production"
    return None


production_supervisor = Agent(
    name="production_supervisor",
    model=build_model(),
    instruction=_INSTRUCTION,
    tools=[
        generate_video_clip_tool,
        probe_clip_tool,
        add_video_clip_tool,
        get_timeline_status_tool,
        validate_timeline_tool,
        provision_gpu_vm_tool,
        check_vm_status_tool,
        terminate_vm_tool,
        list_active_vms_tool,
    ],
    before_agent_callback=_production_phase_setup,
    after_agent_callback=timeline_guardian_callback,
)
