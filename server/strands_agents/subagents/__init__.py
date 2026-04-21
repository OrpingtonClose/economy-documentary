"""SubAgent TypedDicts passed to ``create_deep_agent(subagents=...)``.

See components 09, 10, 13 for the domain partition.
"""

from __future__ import annotations

from strands_agents.subagents.visual import (
    VISUAL_LOOP_BOOTSTRAP_TOOLS,
    VISUAL_LOOP_ITERATION_TOOLS,
    VISUAL_LOOP_MAX_ITERATIONS,
    VISUAL_LOOP_PASS_RATINGS,
    VISUAL_SUBAGENT_DEFAULT_MODEL,
    VISUAL_SUBAGENT_MODEL_ENV,
    VISUAL_SUBAGENT_PROMPT,
    VISUAL_SUBAGENT_TOOL_NAMES,
    VISUAL_SUBAGENT_TOOLS,
    build_visual_subagent,
)

__all__ = [
    "VISUAL_LOOP_BOOTSTRAP_TOOLS",
    "VISUAL_LOOP_ITERATION_TOOLS",
    "VISUAL_LOOP_MAX_ITERATIONS",
    "VISUAL_LOOP_PASS_RATINGS",
    "VISUAL_SUBAGENT_DEFAULT_MODEL",
    "VISUAL_SUBAGENT_MODEL_ENV",
    "VISUAL_SUBAGENT_PROMPT",
    "VISUAL_SUBAGENT_TOOLS",
    "VISUAL_SUBAGENT_TOOL_NAMES",
    "build_visual_subagent",
]
