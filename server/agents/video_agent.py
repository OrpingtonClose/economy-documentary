"""
Video Agent — visual planning + production in one agent.

Architecture::

    VideoAgent
    ├── Visual loop (up to 3 iterations)
    │   ├── Content Analyst      — semantic analysis + visual breakpoints
    │   ├── Visual Concepter     — LTX-2.3 video prompts
    │   └── Coherence Evaluator  — quality gate
    └── Production phase
        ├── Production Planner    — decide what clips to render
        ├── GPU rendering        — dispatch to VM agents
        └── Production Evaluator  — quality check

The video agent reads scenes + alignment from the OTIO agent,
produces visual concepts, writes them to the OTIO agent, then
renders video clips and adds them to the timeline.

All cross-unit communication goes through the OTIO agent:
- READ: scenes, whisperx_alignment from OTIO agent
- WRITE: visual_concepts to OTIO agent
- WRITE: video clips to OTIO agent
- TALK: GPU rendering requests to VM agents via provisioner
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from recovery_agents import AgentTool

logger = logging.getLogger(__name__)


# ── Video Agent tools ────────────────────────────────────────────────

def _tool_read_scenes(tool_context=None) -> str:
    """Read scenes from the OTIO agent."""
    try:
        from agents.otio_agent import _tool_read_pipeline_data
        return _tool_read_pipeline_data("scenes", tool_context)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_read_alignment(tool_context=None) -> str:
    """Read WhisperX alignment from the OTIO agent."""
    try:
        from agents.otio_agent import _tool_read_pipeline_data
        return _tool_read_pipeline_data("whisperx_alignment", tool_context)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_write_visual_concepts(concepts_json: str, provenance_json: str = "{}", tool_context=None) -> str:
    """Write visual concepts to the OTIO agent."""
    try:
        from agents.otio_agent import _tool_write_pipeline_data
        return _tool_write_pipeline_data("visual_concepts", concepts_json, provenance_json, tool_context)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_render_clip(scene_num: int, phrase_idx: int, prompt: str,
                      negative_prompt: str = "", duration: float = 5.0,
                      lora_id: str = "", tool_context=None) -> str:
    """Submit a video clip render job to a GPU worker.

    Talks to the provisioner if no GPU worker is available.
    """
    import os
    import urllib.request
    import urllib.error

    worker_url = os.environ.get("VIDEO_WORKER_URLS", "")
    if not worker_url:
        return json.dumps({
            "error": "No GPU worker available. VIDEO_WORKER_URLS not set.",
            "scene_num": scene_num,
            "phrase_idx": phrase_idx,
        })

    payload = json.dumps({
        "scene_num": scene_num,
        "phrase_idx": phrase_idx,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "duration": duration,
        "lora_id": lora_id,
        "job_type": "video_render",
    }).encode()

    try:
        req = urllib.request.Request(
            f"{worker_url.rstrip('/')}/video/render",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read().decode())
            return json.dumps(result)
    except urllib.error.URLError as e:
        return json.dumps({
            "error": f"GPU worker unreachable: {e}",
            "scene_num": scene_num,
            "phrase_idx": phrase_idx,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_check_clip(job_id: str, tool_context=None) -> str:
    """Check the status of a GPU render job."""
    import os
    import urllib.request
    import urllib.error

    worker_url = os.environ.get("VIDEO_WORKER_URLS", "")
    if not worker_url:
        return json.dumps({"error": "No GPU worker available"})

    try:
        req = urllib.request.Request(
            f"{worker_url.rstrip('/')}/video/status/{job_id}",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_add_video_clip(track: str, scene_num: int, phrase_idx: int,
                         clip_path: str, duration: float,
                         provenance_json: str = "{}", tool_context=None) -> str:
    """Add a rendered video clip to the OTIO timeline."""
    try:
        from agents.otio_agent import _tool_add_clip
        return _tool_add_clip(track, scene_num, phrase_idx, clip_path,
                              duration, provenance_json, tool_context)
    except Exception as e:
        return json.dumps({"error": str(e)})


_VIDEO_AGENT_TOOLS = [
    AgentTool(
        name="read_scenes",
        description="Read the scene plan from the OTIO agent.",
        parameters={"type": "object", "properties": {}, "required": []},
        fn=lambda tool_context=None: _tool_read_scenes(tool_context),
    ),
    AgentTool(
        name="read_alignment",
        description="Read WhisperX alignment from the OTIO agent.",
        parameters={"type": "object", "properties": {}, "required": []},
        fn=lambda tool_context=None: _tool_read_alignment(tool_context),
    ),
    AgentTool(
        name="write_visual_concepts",
        description="Write visual concepts to the OTIO agent with provenance.",
        parameters={
            "type": "object",
            "properties": {
                "concepts_json": {"type": "string", "description": "JSON array of visual concepts"},
                "provenance_json": {"type": "string", "description": "JSON provenance record"},
            },
            "required": ["concepts_json"],
        },
        fn=lambda concepts_json, provenance_json="{}", tool_context=None: _tool_write_visual_concepts(
            concepts_json, provenance_json, tool_context
        ),
    ),
    AgentTool(
        name="render_clip",
        description="Submit a video clip render job to a GPU worker.",
        parameters={
            "type": "object",
            "properties": {
                "scene_num": {"type": "integer"},
                "phrase_idx": {"type": "integer"},
                "prompt": {"type": "string"},
                "negative_prompt": {"type": "string"},
                "duration": {"type": "number"},
                "lora_id": {"type": "string"},
            },
            "required": ["scene_num", "phrase_idx", "prompt"],
        },
        fn=lambda scene_num, phrase_idx, prompt, negative_prompt="", duration=5.0, lora_id="", tool_context=None: _tool_render_clip(
            scene_num, phrase_idx, prompt, negative_prompt, duration, lora_id, tool_context
        ),
    ),
    AgentTool(
        name="check_clip",
        description="Check the status of a GPU render job.",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
            },
            "required": ["job_id"],
        },
        fn=lambda job_id, tool_context=None: _tool_check_clip(job_id, tool_context),
    ),
    AgentTool(
        name="add_video_clip",
        description="Add a rendered video clip to the OTIO timeline.",
        parameters={
            "type": "object",
            "properties": {
                "track": {"type": "string"},
                "scene_num": {"type": "integer"},
                "phrase_idx": {"type": "integer"},
                "clip_path": {"type": "string"},
                "duration": {"type": "number"},
                "provenance_json": {"type": "string"},
            },
            "required": ["track", "scene_num", "phrase_idx", "clip_path", "duration"],
        },
        fn=lambda track, scene_num, phrase_idx, clip_path, duration, provenance_json="{}", tool_context=None: _tool_add_video_clip(
            track, scene_num, phrase_idx, clip_path, duration, provenance_json, tool_context
        ),
    ),
]


class VideoAgent:
    """Video Agent — visual planning + production.

    Reads scenes and alignment from the OTIO agent, produces visual
    concepts, writes them to the OTIO agent, then renders video clips
    and adds them to the timeline.

    All cross-unit communication goes through the OTIO agent.
    """

    def __init__(self) -> None:
        from recovery_agents import RecoveryAgent
        self._agent = RecoveryAgent(
            name="video_agent",
            instruction=(
                "You are the Video Agent for a documentary pipeline.\n\n"
                "Your job has two phases:\n\n"
                "PHASE 1: VISUAL PLANNING\n"
                "1. Read scenes and alignment from the OTIO agent (read_scenes, read_alignment)\n"
                "2. Analyze the narration content — identify semantic breakpoints\n"
                "3. Generate visual concepts (LTX-2.3 prompts) for each visual phrase\n"
                "4. Write visual concepts to the OTIO agent (write_visual_concepts)\n"
                "5. If visual concepts don't pass quality check, iterate\n\n"
                "PHASE 2: PRODUCTION\n"
                "1. For each visual concept, render a video clip (render_clip)\n"
                "2. Check render status (check_clip)\n"
                "3. Add completed clips to the OTIO timeline (add_video_clip)\n"
                "4. If a clip fails, try once more with adjusted parameters\n\n"
                "RULES:\n"
                "- ALL data flows through the OTIO agent. No agent state.\n"
                "- Every write carries provenance.\n"
                "- If scenes or alignment are missing, report the error — that's a contract violation.\n"
                "- If GPU worker is unavailable, report the error.\n"
            ),
            tools=_VIDEO_AGENT_TOOLS,
        )

    def decide(self, context):
        return self._agent.decide(context)


# Module-level instance
video_agent = VideoAgent()
