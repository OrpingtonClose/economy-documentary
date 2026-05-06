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

Usage::

    from strands_agents.stages.assembly_stage import build_assembly_agent

    agent = build_assembly_agent(otio_manager=my_manager)
    result = agent("Assemble the final cut.")
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
from strands_agents.hooks import (
    CheckpointHook,
    QANodeHook,
    ShellGuardHook,
)
from strands_agents.tools.assembly_tool import assemble_final_cut

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — identical text to the ADK assembler_agent
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the Assembler Agent for a documentary pipeline.
Assembly is handled automatically. Report completion.
"""

# ---------------------------------------------------------------------------
# OTIO-aware tool wrappers
# ---------------------------------------------------------------------------


@tool
def read_assembly_state(otio_manager: OTIOStateManager) -> str:
    """Read the OTIO timeline summary for the assembly stage.

    Delegates to :meth:`OTIOStateManager.read` so the LLM sees a
    text summary rather than the raw OTIO object.

    Args:
        otio_manager: The pipeline's OTIO state manager.
    """
    return otio_manager.read("assembly")


@tool
def write_assembly_mutation(
    operation: str,
    details: str,
    otio_manager: OTIOStateManager,
) -> str:
    """Request a guarded mutation on the OTIO timeline.

    The ``otio_manager.guard_mutation`` check is applied before
    any change is allowed. If the timeline is authoritative and
    no escalation is active, the mutation is rejected.

    Args:
        operation: Mutation name (add_clip, remove_clip, replace_clip, etc.).
        details: JSON string with mutation-specific parameters.
        otio_manager: The pipeline's OTIO state manager.
    """
    try:
        otio_manager.guard_mutation(operation)
    except Exception as exc:
        return f"REJECTED: {exc}"
    # Delegate to the domain otio_write tool for the actual mutation
    return otio_write(operation, details)


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

    The agent carries the ``assemble_final_cut`` deterministic leaf
    tool plus OTIO read/write tools so the LLM can inspect the
    timeline and request mutations (guarded by the OTIO state
    manager's lifecycle rules).

    ADK callback mapping:

    * ``deterministic_assembly_callback`` (before_agent) → the
      ``assemble_final_cut`` tool is the deterministic leaf; the
      LLM calls it under the system prompt's instruction.
    * ``timeline_guardian_callback`` (after_agent) →
      :class:`CheckpointHook` + :class:`QANodeHook` wired on the
      agent enforce post-assembly validation and checkpointing.

    Args:
        otio_manager: The pipeline's :class:`OTIOStateManager`.
            When ``None``, the agent's OTIO tools will return
            placeholder messages (suitable for offline testing).
        model: Any value accepted by ``strands.Agent(model=...)``.
            When ``None`` the SDK falls through to its default.
        window_size: Messages kept by the
            :class:`SlidingWindowConversationManager`. Assembly
            is a single-shot tool call, so 10 is more than
            sufficient.
        enforce_checkpoint: When True, wire :class:`CheckpointHook`
            to snapshot OTIO state to B2 after the agent runs.
        enforce_qa: When True, wire :class:`QANodeHook` to run
            QA checks after the agent runs.
        enforce_shell_guard: When True, wire :class:`ShellGuardHook`
            to enforce command allowlisting on shell tools.

    Returns:
        Configured :class:`Agent` ready for ``.__call__`` invocations.
    """
    hooks: list[Any] = []
    if enforce_checkpoint:
        hooks.append(CheckpointHook())
    if enforce_qa:
        hooks.append(QANodeHook())
    if enforce_shell_guard:
        hooks.append(ShellGuardHook())

    # Build the tool list. When otio_manager is provided, the
    # OTIO-aware wrappers close over it; otherwise the domain
    # otio_tools (which return placeholders) are used directly.
    if otio_manager is not None:
        otio_tools: list[Any] = [
            _bind_otio_manager(read_assembly_state, otio_manager),
            _bind_otio_manager(write_assembly_mutation, otio_manager),
            update_navigation,
            shell_safe,
        ]
    else:
        otio_tools = [
            otio_read,  # falls back to placeholder
            otio_write,
            update_navigation,
            shell_safe,
        ]

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


# ---------------------------------------------------------------------------
# Helper — bind OTIOStateManager to tool via closure
# ---------------------------------------------------------------------------


def _bind_otio_manager(tool_fn: Any, otio_manager: OTIOStateManager) -> Any:
    """Return a tool function with ``otio_manager`` pre-bound.

    Strands ``@tool`` functions are invoked by the agent runtime
    with only the arguments the LLM supplies. The ``otio_manager``
    is a runtime dependency (not an LLM-visible parameter), so we
    close over it via a wrapper that strips it from the tool's
    input schema.

    The wrapper preserves the original tool's ``tool_spec`` so the
    LLM sees the correct parameter list (without ``otio_manager``).
    """
    import functools

    @functools.wraps(tool_fn)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        kwargs["otio_manager"] = otio_manager
        return tool_fn(*args, **kwargs)

    # Copy the Strands tool_spec so the LLM sees the right schema
    # (minus the otio_manager parameter).
    if hasattr(tool_fn, "tool_spec"):
        original_spec = tool_fn.tool_spec
        # Remove otio_manager from the input schema so the LLM
        # doesn't try to supply it.
        filtered_spec = _strip_otio_manager_from_spec(original_spec)
        _wrapper.tool_spec = filtered_spec
    elif hasattr(tool_fn, "tool_name"):
        _wrapper.tool_name = tool_fn.tool_name

    return _wrapper


def _strip_otio_manager_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Remove ``otio_manager`` from a tool_spec's inputSchema.

    The ``otio_manager`` parameter is a runtime dependency, not
    something the LLM should supply. This function returns a copy
    of the spec with ``otio_manager`` removed from ``required``
    and ``properties``.
    """
    import copy

    spec = copy.deepcopy(spec)
    input_schema = spec.get("inputSchema", {})
    json_schema = input_schema.get("json", {})
    properties = json_schema.get("properties", {})
    if "otio_manager" in properties:
        del properties["otio_manager"]
    required = json_schema.get("required", [])
    if "otio_manager" in required:
        json_schema["required"] = [r for r in required if r != "otio_manager"]
    return spec


__all__ = [
    "build_assembly_agent",
    "read_assembly_state",
    "write_assembly_mutation",
]
