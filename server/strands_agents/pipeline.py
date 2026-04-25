"""Documentary pipeline orchestrator — component 14.

One ``create_deep_agent`` call wiring leaves (Strands tools), SubAgents
(cohesive domains), memory (``AGENTS.md``), and ``interrupt_on``
(approval gates). Replaces the 1 111-line ``server/agents/pipeline.py``.

The orchestrator is built with a **dependency-injection signature** —
callers pass in the tool and SubAgent lists they want. This keeps
component 14 runnable off ``main`` even while the per-component PRs
(01 – 13) are still open: each caller supplies whatever is available.
:func:`build_default_tools` and :func:`build_default_subagents` do a
best-effort import of the real leaves; anything missing is replaced by
the thin placeholder in :mod:`server.strands_agents._placeholders`.

The strangler-fig policy is unchanged: ``server/agents/pipeline.py`` is
untouched. This module is only reachable via ``--pipeline=strands`` and
:func:`server.strands_agents.run.run_documentary`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from deepagents import SubAgent, create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

from . import _placeholders
from .approval import INTERRUPT_GATE_CONFIG, request_human_approval

logger = logging.getLogger(__name__)


ORCHESTRATOR_PROMPT = """\
You are the documentary pipeline orchestrator. Your job is to turn a user
brief into a final video, going through five stages:

1. Scenario — call generate_scenario, then evaluate_scenario, then
   refine_scenario until the scenario passes structural checks.
2. Audio + timing — launch_audio_render in parallel per scene, await,
   evaluate_timing, and loop back to refine_scenario when timing fails
   (see AGENTS.md "Timing stage").
3. Visual — delegate to the `visual` SubAgent via the task tool.
4. Production — delegate to the `production` SubAgent via the task
   tool.
5. Assembly — launch_assembly, await, then launch_b2_sync.

Approval gates (handled by interrupt_on): launch_visual_production,
launch_assembly, request_human_approval. Do not try to bypass them.

When anything fails: first try tactical recovery inside the owning
SubAgent. If that exhausts, delegate to the `escalation` SubAgent and
follow its decision. Never mark a stage complete with unresolved
failures (see AGENTS.md invariants).
"""


# The set of tool names that must trigger a LangGraph interrupt before
# their body runs. ``HumanInTheLoopMiddleware`` (wired by
# ``create_deep_agent`` via ``interrupt_on``) turns each call into an
# interrupt the operator resumes with a ``Command``.
INTERRUPT_TOOL_NAMES: tuple[str, ...] = (
    "launch_visual_production",
    "launch_assembly",
    "request_human_approval",
)


_DEFAULT_MEMORY_PATHS: tuple[str, ...] = (
    "docs/strands-migration/AGENTS.md",
    ".deepagents/AGENTS.md",
)


_Tool = BaseTool | Callable[..., Any] | dict[str, Any]


def _try_import(module_path: str, attr: str) -> Any | None:
    """Best-effort import. Returns ``None`` when the symbol is absent.

    Swallows ``ImportError`` / ``AttributeError`` only. Any other
    exception propagates.
    """

    try:
        module = __import__(module_path, fromlist=[attr])
        return getattr(module, attr, None)
    except ImportError:
        return None


def build_default_tools() -> list[_Tool]:
    """Assemble the default tool list for the orchestrator.

    Imports each real leaf if its PR has merged; falls back to the
    corresponding placeholder otherwise. The DI entrypoint
    (:func:`build_orchestrator`) does **not** call this — it is only
    invoked by :func:`build_documentary_orchestrator`, the convenience
    wrapper. Tests pass their own tool list directly so this mapping
    is never traversed in CI.
    """

    def _pick(real_path: str, attr: str, placeholder: Any) -> _Tool:
        real = _try_import(real_path, attr)
        return real or placeholder

    return [
        _pick(
            "server.strands_agents.subagents.scenario_agent",
            "generate_scenario",
            _placeholders.generate_scenario,
        ),
        _pick(
            "server.strands_agents.subagents.scenario_agent",
            "evaluate_scenario",
            _placeholders.evaluate_scenario,
        ),
        _pick(
            "server.strands_agents.subagents.scenario_refiner",
            "refine_scenario",
            _placeholders.refine_scenario,
        ),
        _pick(
            "server.strands_agents.subagents.timing_evaluator",
            "evaluate_timing",
            _placeholders.evaluate_timing,
        ),
        _pick(
            "server.strands_agents.subagents.audio_agent",
            "launch_audio_render",
            _placeholders.launch_audio_render,
        ),
        _pick(
            "server.strands_agents.subagents.production_supervisor",
            "launch_visual_production",
            _placeholders.launch_visual_production,
        ),
        _pick(
            "server.strands_agents.subagents.assembly_agent",
            "launch_assembly",
            _placeholders.launch_assembly,
        ),
        _placeholders.launch_b2_sync,
        _placeholders.check_tasks,
        _placeholders.await_tasks,
        request_human_approval,
    ]


def build_default_subagents() -> list[SubAgent]:
    """Assemble the default SubAgent list for the orchestrator.

    Uses the real visual/production/escalation SubAgents when their
    PRs have merged; otherwise drops them silently. Tests pass their
    own SubAgent list directly.
    """

    out: list[SubAgent] = []
    for module_path, attr in (
        ("server.strands_agents.subagents.visual", "visual_subagent"),
        (
            "server.strands_agents.subagents.production_supervisor",
            "production_subagent",
        ),
        ("server.strands_agents.subagents.escalation", "escalation_subagent"),
    ):
        sub = _try_import(module_path, attr)
        if sub is not None:
            out.append(sub)
    return out


def _build_interrupt_on(
    tool_names: Sequence[str],
) -> dict[str, bool | dict[str, Any]]:
    """Turn the orchestrator's sensitive-tool list into ``interrupt_on``.

    For tools registered in :data:`server.strands_agents.approval.INTERRUPT_GATE_CONFIG`
    we reuse the per-gate ``allowed_decisions`` from component 15
    (e.g. ``launch_assembly`` drops ``edit``). Unknown tool names
    fall back to the superset (``accept`` / ``edit`` / ``reject`` /
    ``respond``) so callers can extend the gate set without touching
    the canonical table.
    """

    default_allowed = ["accept", "edit", "reject", "respond"]
    config: dict[str, bool | dict[str, Any]] = {}
    for name in tool_names:
        known = INTERRUPT_GATE_CONFIG.get(name)
        allowed = (
            list(known.get("allowed_decisions", default_allowed))
            if known is not None
            else default_allowed
        )
        config[name] = {"allowed_decisions": allowed}
    return config


def build_orchestrator(
    run_dir: Path,
    *,
    model: str | BaseChatModel | None = None,
    tools: Sequence[_Tool] | None = None,
    subagents: Sequence[SubAgent] | None = None,
    memory: Sequence[str] | None = None,
    system_prompt: str = ORCHESTRATOR_PROMPT,
    interrupt_tool_names: Sequence[str] = INTERRUPT_TOOL_NAMES,
    interrupt_on: dict[str, bool | dict[str, Any]] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> Any:
    """Build the documentary pipeline DeepAgent.

    Keyword-only arguments let tests inject a mocked chat model, a
    minimal tool list, and stub SubAgents without pulling in the
    full leaf surface. The production entrypoint
    (:func:`build_documentary_orchestrator`) wires the defaults.

    Args:
        run_dir: Filesystem root the DeepAgent's
            :class:`FilesystemBackend` operates on. The orchestrator
            reads AGENTS.md from here and writes scratch files here.
        model: Chat model id (e.g. ``"openai/gpt-4o"``) or a
            pre-built :class:`BaseChatModel`. Defaults to the value
            of ``STRANDS_MODEL`` or ``openai/gpt-4o``.
        tools: Tool list. When ``None`` no tools are registered —
            callers that want the default leaf surface should call
            :func:`build_documentary_orchestrator` instead.
        subagents: SubAgent list. Same defaulting as ``tools``.
        memory: Paths passed to ``MemoryMiddleware``. Defaults to
            :data:`_DEFAULT_MEMORY_PATHS`.
        system_prompt: Orchestrator system prompt. Defaults to
            :data:`ORCHESTRATOR_PROMPT`.
        interrupt_tool_names: Names of tools that must be gated by
            an approval interrupt. Defaults to
            :data:`INTERRUPT_TOOL_NAMES`.

    Returns:
        A compiled LangGraph ``CompiledStateGraph`` ready to invoke.
    """

    resolved_model: str | BaseChatModel = (
        model if model is not None else os.environ.get("STRANDS_MODEL", "openai/gpt-4o")
    )
    resolved_tools: list[_Tool] = list(tools) if tools is not None else []
    resolved_subagents: list[SubAgent] = (
        list(subagents) if subagents is not None else []
    )
    resolved_memory: list[str] = (
        list(memory) if memory is not None else list(_DEFAULT_MEMORY_PATHS)
    )

    logger.info(
        "tool_count=<%d>, subagent_count=<%d>, interrupt_count=<%d>, "
        "memory_count=<%d> | build_orchestrator",
        len(resolved_tools),
        len(resolved_subagents),
        len(interrupt_tool_names),
        len(resolved_memory),
    )

    resolved_interrupt_on = (
        interrupt_on
        if interrupt_on is not None
        else _build_interrupt_on(interrupt_tool_names)
    )

    return create_deep_agent(
        model=resolved_model,
        tools=resolved_tools,
        system_prompt=system_prompt,
        subagents=resolved_subagents,
        memory=resolved_memory,
        backend=FilesystemBackend(root_dir=str(run_dir)),
        interrupt_on=resolved_interrupt_on,
        checkpointer=checkpointer,
    )


def build_documentary_orchestrator(
    run_dir: Path,
    *,
    model: str | BaseChatModel | None = None,
) -> Any:
    """Convenience wrapper that wires the default leaves + SubAgents.

    Equivalent to calling :func:`build_orchestrator` with
    ``tools=build_default_tools()`` and
    ``subagents=build_default_subagents()``. Placeholders fill in
    wherever a per-component PR has not yet merged.
    """

    return build_orchestrator(
        run_dir,
        model=model,
        tools=build_default_tools(),
        subagents=build_default_subagents(),
    )


__all__ = [
    "INTERRUPT_TOOL_NAMES",
    "ORCHESTRATOR_PROMPT",
    "build_default_subagents",
    "build_default_tools",
    "build_documentary_orchestrator",
    "build_orchestrator",
]
