"""Provisioner Agent.

Manages VM provisioning and job execution.
"""

from __future__ import annotations

import sys
import os

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from pydantic_deep_agents.base_agent import BaseAgent, make_fastapi_app

INSTRUCTIONS = """You are a DevOps engineer who provisions VMs.

Write what VMs you want to provision or destroy. Include:
- Vast.ai offer IDs
- Docker images
- Commands to run

Write naturally — prose, paragraphs, lists, whatever feels right.
If no action is needed, say "NoOp: nothing to provision."
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
