"""Audio Agent.

Writes audio job requests in free text. Instructor parses into GenerateNarrationAudio.
"""

from __future__ import annotations

import sys
import os

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from pydantic_deep_agents.base_agent import BaseAgent, make_fastapi_app

INSTRUCTIONS = """You are an audio producer for documentaries.

Write which narration audio jobs you want created. Include:
- Scene number
- Voice (V1, V2, V3)
- The exact narration text to synthesize

Write naturally — prose, paragraphs, lists, whatever feels right.
If no script exists or jobs already exist, say "NoOp: waiting."
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
