"""Assembly Agent.

Merges clips and renders final output.
"""

from __future__ import annotations

import sys
import os

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from pydantic_deep_agents.base_agent import BaseAgent, make_fastapi_app

INSTRUCTIONS = """You are a video editor who assembles documentaries.

Write what you want to merge and render. Include:
- Audio clip paths and scene numbers
- Video clip paths and scene numbers
- Any ffmpeg commands needed

Write naturally — prose, paragraphs, lists, whatever feels right.
If audio/video is not ready, say "NoOp: waiting for media."
"""

_agent = BaseAgent(
    model="deepseek:deepseek-v4-flash",
    instructions=INSTRUCTIONS,
    include_memory=True,
    include_subagents=False,
    web_search=False,
    web_fetch=False,
    thinking=False,
)

app = make_fastapi_app(_agent)
