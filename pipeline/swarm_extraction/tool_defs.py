"""
Native tool definitions for LLM function calling.

Re-exports tool definitions from tools.py and adds any additional
tool definitions needed for the enrichment pipeline.
"""

from __future__ import annotations

from .tools import TOOL_DEFINITIONS

# NATIVE_TOOLS is the list of all tool definitions in OpenAI function-calling
# format, used by the verification subagent for native tool calling.
NATIVE_TOOLS = TOOL_DEFINITIONS
