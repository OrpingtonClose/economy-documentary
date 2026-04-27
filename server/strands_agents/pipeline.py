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
from .qa_gates import (
    qa_audio_completeness,
    qa_duration_align,
    qa_stills_judge,
    qa_video_artifact_probe,
)

logger = logging.getLogger(__name__)


ORCHESTRATOR_PROMPT = """\
You are the documentary pipeline orchestrator. Your job is to turn a user
brief into a final video, going through five stages.

Multi-scene iteration discipline (slice 9f). The scenario produced in
stage 1 has N scenes. Stages 2 / 3 / 4 must dispatch one tool call per
scene — never collapse N scenes to a single call. Concretely:

- Emit ALL ``launch_audio_render`` calls for the N scenes on the SAME
  turn, as parallel ``tool_calls`` on one assistant message (AGENTS.md
  "Timing stage" — batch launches). Then a single ``await_tasks`` /
  ``evaluate_timing`` for the whole batch.
- Emit ALL ``launch_visual_production`` calls for the N scenes on the
  SAME turn, as parallel ``tool_calls``. The HITL approval gate fires
  per call — that is expected, do not try to merge calls to dodge it.
- ``propose_visual_concept`` (when available) and the visual SubAgent
  helpers run once per scene; serial within a scene is fine, but
  every scene must get its own concept.

Stages:

1. Scenario — call generate_scenario, then evaluate_scenario, then
   refine_scenario until the scenario passes structural checks.
2. Audio + timing — launch_audio_render in parallel per scene, await,
   evaluate_timing, and loop back to refine_scenario when timing fails
   (see AGENTS.md "Timing stage"). Always pass the scene's narration
   string from the approved scenario as the ``text`` argument to
   launch_audio_render so the TTS renders the actual script (slice 9c).

   After every successful ``launch_audio_render`` and BEFORE moving to
   ``evaluate_timing``, you MUST call ``qa_audio_completeness`` (with
   the ``audio_path`` from the audio dispatcher envelope). This gate
   is non-negotiable per AGENTS.md hard invariants §3 and §5
   ("fail closed on TTS", "QA immediately after each artifact"). It
   detects the Qwen3-TTS abrupt-cut failure mode (narration sliced
   mid-utterance when the model exhausts its decoded-token budget) by
   checking trailing silence + end-of-file RMS energy. If the gate
   returns ``verdict == "fail"``, do NOT proceed to ``evaluate_timing``
   — delegate to the ``escalation`` SubAgent via the task tool with
   the failed gate's envelope and the dispatcher envelope. Never
   silently accept a failed audio gate; never re-run the same audio
   dispatch with the same args without an escalation decision.
3. Visual — delegate to the `visual` SubAgent via the task tool.
   When the propose_visual_concept tool is available (slice
   9c-LLM-visual gate set), call it once per scene with the scene's
   first phrase, the project style_lock, and visual_style. Use the
   returned ``prompt`` string and ``visual_concept`` dict directly
   in the next stage.
4. Production — delegate to the `production` SubAgent via the task
   tool. When calling launch_visual_production, pass a fully-formed
   style-locked ``prompt`` string built from the scene's
   visual_concept (shot type, camera movement, mood, palette,
   phrases) so the video model receives a rich description, not a
   one-line caption (slice 9c). When propose_visual_concept ran in
   stage 3, prefer its returned prompt verbatim. Also pass the
   scene's narration duration (from ``evaluate_timing``'s
   ``alignment_per_scene[i].duration_sec`` or the scenario scene's
   ``duration_target_sec``) as ``target_duration_s`` so the video
   model renders enough frames to cover the audio (slice 9k —
   without this argument LTX-2.3 emits a fixed ~3.7 s clip and the
   muxer freezes the last frame). Dispatch one call per scene in
   parallel (slice 9f).

   After every successful ``launch_visual_production`` and BEFORE
   moving to assembly, you MUST call BOTH ``qa_duration_align``
   (with the scene's ``audio_path`` from the audio dispatcher
   envelope and ``video_path`` from the visual dispatcher envelope)
   AND ``qa_stills_judge`` (with the same ``video_path``). These
   gates are non-negotiable per AGENTS.md hard invariants §3-5
   ("fail closed on video render", "QA immediately after each
   artifact"). If either gate returns ``verdict == "fail"``, do NOT
   proceed to assembly — delegate to the ``escalation`` SubAgent
   via the task tool with a payload containing the failed gate's
   envelope, the scene_id, and the dispatcher envelopes. Follow
   the escalation decision (``retry`` / ``fix`` / ``skip`` /
   ``escalate_to_human`` / ``abort``). Never silently accept a
   failed QA gate; never re-run the same dispatch with the same
   args without an escalation decision.
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
        qa_audio_completeness,
        qa_duration_align,
        qa_stills_judge,
        qa_video_artifact_probe,
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

    When ``QWEN3_TTS_WORKER_URL`` and/or ``LTX_VIDEO_WORKER_URL``
    are set in the environment, the corresponding placeholder tools
    (``launch_audio_render`` / ``launch_visual_production``) are
    swapped for real-worker HTTP dispatchers that POST to the live
    workers and persist returned bytes under
    ``run_dir/artifacts/``. Both URLs unset → all placeholders
    pass through, matching pre-slice-9e behaviour exactly.

    When ``model`` is a string id (or ``STRANDS_MODEL`` /
    ``SCENARIO_LLM_MODEL_ID`` is set), the scenario placeholders
    (``generate_scenario`` / ``evaluate_scenario`` / ``refine_scenario``)
    are swapped for real LLM-backed tools that delegate to
    :mod:`scenario_llm` for narration generation and
    :mod:`tools.scenario_evaluator_checks` for structural checks
    (slice 9c-LLM-scenario). Without a model id resolution all
    scenario placeholders pass through.

    When ``model`` is a string id (or ``STRANDS_MODEL`` /
    ``VISUAL_LLM_MODEL_ID`` is set), an additional
    ``propose_visual_concept`` tool is appended to the orchestrator's
    tool list. The orchestrator calls it per scene to obtain a real,
    style-locked LTX prompt + structured visual concept dict that
    feeds straight into ``launch_visual_production`` (slice
    9c-LLM-visual). Without a model id resolution the tool is
    omitted and the orchestrator falls back to constructing concepts
    inline from the scenario's ``visual_notes``.
    """
    from ._real_scenario_tools import (
        apply_real_scenario_overrides,
        build_real_scenario_tools,
    )
    from ._real_visual_tools import (
        apply_real_visual_overrides,
        build_real_visual_tools,
    )
    from .playground.pipeline_live_real_workers import (
        apply_real_worker_overrides,
        build_real_worker_tools,
    )

    base_tools = build_default_tools()
    llm_model_id = model if isinstance(model, str) else None
    scenario_overrides = build_real_scenario_tools(model_id=llm_model_id)
    tools = apply_real_scenario_overrides(base_tools, scenario_overrides)
    visual_overrides = build_real_visual_tools(model_id=llm_model_id)
    tools = apply_real_visual_overrides(tools, visual_overrides)
    real_overrides = build_real_worker_tools(run_dir)
    tools = apply_real_worker_overrides(tools, real_overrides)
    return build_orchestrator(
        run_dir,
        model=model,
        tools=tools,
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
