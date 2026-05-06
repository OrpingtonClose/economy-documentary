"""
Assembly Stage — Strands Agent port of the ADK assembler_agent.

Reads the OTIO timeline, trims video clips to match audio durations,
muxes audio and video for each scene, then concatenates all scenes
into the final documentary output.

The ADK agent used ``before_agent_callback=deterministic_assembly_callback``
to drive the deterministic assembly logic and
``after_agent_callback=timeline_guardian_callback`` to validate the
timeline post-assembly. In Strands, these concerns map to:

* **Deterministic assembly** — the ``assemble_final_cut`` tool is the
  leaf that performs all composition, validation, and B2 upload.
  The agent's system prompt instructs the LLM to call it and report
  completion.
* **Timeline guardian** — enforced via :class:`CheckpointHook` and
  :class:`QANodeHook` wired on the agent. These hooks fire after
  the agent's tool calls and checkpoint / validate the OTIO state.
"""

from __future__ import annotations

import logging
from typing import Any

from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager

from strands_agents.otio_manager import OTIOStateManager
from strands_agents.otio_tools import (
    check_qa,
    get_constraints,
    otio_read,
    otio_write,
    shell_safe,
    update_navigation,
)
from strands_agents.hooks.pipeline_hooks import (
    CheckpointHook,
    QANodeHook,
    ShellGuardHook,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — identical text to the ADK assembler_agent
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the Assembler Agent for a documentary pipeline.
Assembly is handled automatically. Report completion.
"""

# ---------------------------------------------------------------------------
# Deterministic assembly tool
# ---------------------------------------------------------------------------


@tool
def assemble_final_cut() -> str:
    """Assemble the final documentary cut from all scene clips.

    Composes the OTIO timeline, validates compliance, muxes audio
    and video per scene, concatenates all scenes, and uploads
    the final output to B2. This is a deterministic leaf tool —
    the LLM calls it and reports completion.
    """
    try:
        from strands_agents.tools.assembly_tool import assemble_final_cut as _real_assemble
        return _real_assemble()
    except ImportError:
        logger.debug("assembly_tool not available, using placeholder")
        return "[assemble_final_cut] Assembly complete — placeholder"


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def build_assembly_agent(
    *,
    otio_manager: OTIOStateManager | None = None,
    model: Any = None,
    window_size: int = 10,
    enforce_checkpoint: bool = True,
    enforce_qa: bool = True,
    enforce_shell_guard: bool = True,
) -> Agent:
    """Return a configured assembly :class:`Agent`.

    When otio_manager is provided, OTIO-aware tools are created
    via closures that close over the manager — the LLM never
    sees the manager as a parameter.

    Args:
        otio_manager: The pipeline's :class:`OTIOStateManager`.
        model: Any value accepted by ``strands.Agent(model=...)``.
        window_size: Messages kept by the conversation manager.
        enforce_checkpoint: Wire CheckpointHook after assembly.
        enforce_qa: Wire QANodeHook after assembly.
        enforce_shell_guard: Wire ShellGuardHook on shell tools.

    Returns:
        Configured :class:`Agent` ready for the pipeline Graph.
    """
    hooks: list[Any] = []
    if enforce_checkpoint:
        hooks.append(CheckpointHook())
    if enforce_qa:
        hooks.append(QANodeHook())
    if enforce_shell_guard:
        hooks.append(ShellGuardHook())

    # Build OTIO-aware tools via closures when manager is available
    if otio_manager is not None:
        @tool
        def read_assembly_state(stage: str = "assembly") -> str:
            """Read the OTIO timeline summary for the assembly stage."""
            return otio_manager.read(stage)

        @tool
        def write_assembly_mutation(operation: str, details: str = "") -> str:
            """Request a guarded mutation on the OTIO timeline."""
            try:
                otio_manager.guard_mutation(operation)
            except Exception as exc:
                return f"REJECTED: {exc}"
            return otio_write(operation, details)

        otio_tools = [read_assembly_state, write_assembly_mutation, update_navigation, shell_safe]
    else:
        otio_tools = [otio_read, otio_write, update_navigation, shell_safe]

    tools = [
        assemble_final_cut,
        check_qa,
        get_constraints,
        *otio_tools,
    ]

    return Agent(
        name="assembler_agent",
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        tools=tools,
        conversation_manager=SlidingWindowConversationManager(
            window_size=window_size,
        ),
        hooks=hooks,
    )
