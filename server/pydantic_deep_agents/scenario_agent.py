"""Scenario Agent.

Writes documentary scripts in free text. Instructor parses into UpdateScript.
"""

from __future__ import annotations

import sys
import os

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from pydantic_deep_agents.base_agent import BaseAgent, make_fastapi_app

INSTRUCTIONS = """You are a documentary scriptwriter.

Write the script in natural language. Include:
- Narration text (3 versions: V1 primary, V2 alternate, V3 third take)
- Visual notes describing shots
- A dopamine hook for the opening
- Pronunciation hints for tricky words
- Duration estimate

Write naturally — prose, paragraphs, whatever feels right.
If the script already exists and looks good, say "NoOp: script already exists."
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
