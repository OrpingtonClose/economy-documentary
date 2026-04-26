"""Demo orchestrator builder for the playground's live pipeline run.

Slice 9a runs the **real** ``create_deep_agent`` orchestrator end-to-end
on the playground without touching real GPU workers or paying for LLM
tokens. To make that possible we build the orchestrator with two
production-shaped wirings and one scripted wiring:

* **Production-shaped** — the same :func:`build_orchestrator` factory,
  the same ``interrupt_on`` middleware, the same memory paths, the
  same placeholder tools the orchestrator falls back to when the
  per-leaf PRs have not landed yet. This is the surface the live
  runner observes via callbacks and interrupt resolution.

* **Production-shaped** — the placeholder tools' return values shape
  themselves like the eventual real tools: ``launch_audio_render``
  returns ``{"task_id": …}``, ``launch_assembly`` returns
  ``{"master_mp4": {"b2_url": …}}``. The runner can therefore extract
  the final MP4 URL from terminal state without special-casing the
  demo path. (Real placeholders today already match this shape — see
  :mod:`strands_agents._placeholders`.)

* **Scripted** — the chat model is a
  :class:`_ScriptedToolCallingModel` borrowed from the trajectory
  simulator. We hand it a deterministic sequence of
  :class:`~langchain_core.messages.AIMessage` envelopes each carrying
  the ``tool_calls`` the orchestrator should make next. After the
  last tool returns, the script ends with an empty ``AIMessage``
  whose content quotes the master MP4 URL the runner reports as the
  final artifact.

The script walks all five canonical stages exactly once (no timing
loop; the timing-loop trajectory has its own dedicated simulator —
this builder just proves the full happy path executes end-to-end) and
fires both approval gates so the playground UI can demonstrate the
operator-resume folding live.

Public surface:

* :func:`build_demo_live_agent` — assemble an orchestrator + scripted
  chat model wired for a single happy-path run on ``run_dir`` with
  ``topic`` / ``target_duration_sec`` / ``language``. Returns a
  compiled LangGraph ready to hand to
  :class:`LivePipelineRun`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from strands_agents import _placeholders
from strands_agents.approval import request_human_approval
from strands_agents.pipeline import build_orchestrator
from strands_agents.playground.pipeline_live_real_workers import (
    apply_real_worker_overrides,
    build_real_worker_tools,
)

logger = logging.getLogger(__name__)


@tool
def content_analyst(
    scene_id: str,
    timeline_excerpt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Demo-only stub for the visual SubAgent's content_analyst tool.

    Real production runs delegate visual analysis to an internal
    SubAgent and never expose ``content_analyst`` at the orchestrator
    tool layer. The demo flattens that delegation so the live runner
    can observe the ``visual`` stage from a top-level tool call,
    proving the stage ribbon walks scenario → audio → visual →
    production → assembly without spinning up the real SubAgent.
    """
    return {
        "tool": "content_analyst",
        "scene_id": scene_id,
        "phrases_per_scene": {scene_id: ["documentary establishing shot"]},
        "timeline_excerpt": timeline_excerpt or {},
    }


@tool
def visual_concepter(
    scene_id: str,
    phrases: list[str] | None = None,
) -> dict[str, Any]:
    """Demo-only stub for the visual SubAgent's visual_concepter tool.

    Same rationale as :func:`content_analyst`: flattens the visual
    SubAgent so the live runner sees a ``visual`` stage bracket.
    """
    return {
        "tool": "visual_concepter",
        "scene_id": scene_id,
        "concepts": [
            {
                "shot_count": 1,
                "style": "documentary",
                "phrases": phrases or ["documentary establishing shot"],
            }
        ],
    }


class _ScriptedToolCallingModel(FakeMessagesListChatModel):
    """:class:`FakeMessagesListChatModel` that accepts ``bind_tools``.

    Vendored locally so this demo module stays decoupled from the
    heavyweight :mod:`strands_agents.sim` substrate (which transitively
    imports the c01..c10 contract surface). The LangGraph tool-calling
    agent calls ``bind_tools`` on its chat model before every LLM step;
    vanilla :class:`FakeMessagesListChatModel` raises
    ``NotImplementedError`` on that call, halting the agent before any
    tool fires. Returning ``self`` ignores the binding — fine here
    because the scripted responses already encode the tool calls we
    want the agent to make.
    """

    def bind_tools(
        self,
        tools: Sequence[Any],
        **kwargs: Any,  # noqa: ARG002 — ignored; scripted responses decide
    ) -> Runnable[Any, Any]:
        _ = tools  # unused — the script decides which tools are called
        return self

    def with_structured_output(
        self,
        schema: Any,
        **kwargs: Any,  # noqa: ARG002 — ignored; scripted responses decide
    ) -> Runnable[Any, Any]:
        _ = schema
        return self


def _ai_tool_call(name: str, args: dict[str, Any]) -> AIMessage:
    """One AIMessage with one ``tool_calls`` entry for ``name``.

    Mirrors :func:`strands_agents.sim.orchestrator_simulator.scripted_tool_call`
    but stays decoupled from the trajectory simulator's helper imports
    so this builder can move independently.
    """
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": args,
                "id": f"call_{uuid4().hex[:12]}",
                "type": "tool_call",
            }
        ],
    )


def _ai_final(content: str) -> AIMessage:
    """Stop-the-loop AIMessage. No ``tool_calls`` → agent terminates."""
    return AIMessage(content=content)


def _demo_chat_script(
    topic: str,
    target_duration_sec: int,
    language: str,
) -> list[AIMessage]:
    """Build the deterministic chat script for the happy-path run.

    Each AIMessage triggers exactly one tool call (no parallel
    launches in the demo — the simulator doesn't model the timing
    loop here). The sequence walks scenario → audio → visual →
    production → assembly → b2_sync, fires every approval gate, and
    ends with a final-answer message quoting the master MP4 URL so
    :func:`LivePipelineRun._scrape_final_mp4_url` can recover it.
    """
    scene_id = "scene_001"
    final_mp4_url = (
        f"b2://documentary/{topic.replace(' ', '_') or 'documentary'}/"
        f"master_mp4/r0001.mp4"
    )
    scene_payload = {"id": scene_id, "duration_sec": float(target_duration_sec)}
    timeline_payload = {"scenes": [scene_id]}
    visual_concept = {
        "shot_count": 1,
        "style": "documentary",
        "shot_type": "establishing wide",
        "camera_movement": "slow dolly in",
        "mood": "grounded, authoritative",
        "palette": "muted earth tones",
        "phrases": [
            f"Cinematic documentary establishing shot exploring {topic}",
            "soft natural light, archival texture, restrained colour grading",
        ],
    }
    narration_text = (
        f"In this opening scene we examine {topic}. "
        "Across the next sixty seconds we trace how it shapes everyday "
        "life, why it matters now, and what it tells us about the road "
        "ahead."
    )
    visual_prompt = (
        f"Cinematic documentary establishing shot exploring {topic}. "
        "Slow dolly in on a grounded subject, soft natural light, "
        "muted earth-tone palette, archival texture, restrained "
        "colour grading. 24fps, 1280x704."
    )
    return [
        _ai_tool_call(
            "generate_scenario",
            {
                "topic": topic,
                "num_scenes": 1,
                "style": "documentary",
                "language": language,
            },
        ),
        _ai_tool_call(
            "evaluate_scenario",
            {
                "scenes": [scene_payload],
                "target_duration_sec": float(target_duration_sec),
            },
        ),
        _ai_tool_call(
            "launch_audio_render",
            {
                "scene_id": scene_id,
                "voice_id": "Ryan",
                "text": narration_text,
            },
        ),
        _ai_tool_call(
            "evaluate_timing",
            {
                "timeline": timeline_payload,
                "alignment": {},
                "target_duration_sec": float(target_duration_sec),
            },
        ),
        _ai_tool_call(
            "content_analyst",
            {"scene_id": scene_id, "timeline_excerpt": timeline_payload},
        ),
        _ai_tool_call(
            "visual_concepter",
            {
                "scene_id": scene_id,
                "phrases": ["documentary establishing shot"],
            },
        ),
        _ai_tool_call(
            "launch_visual_production",
            {
                "scene_id": scene_id,
                "visual_concept": visual_concept,
                "prompt": visual_prompt,
            },
        ),
        _ai_tool_call(
            "launch_assembly",
            {"timeline": timeline_payload, "output_path": final_mp4_url},
        ),
        _ai_tool_call("launch_b2_sync", {"artifact_path": final_mp4_url}),
        _ai_final(f"Final master MP4: {final_mp4_url}"),
    ]


def _demo_tools() -> list[Any]:
    """Tool list the demo orchestrator binds.

    Matches :func:`strands_agents.pipeline.build_default_tools` shape
    but skips the best-effort real-leaf imports — the demo always uses
    placeholders so the build is fully deterministic regardless of
    which per-leaf PRs have landed locally. The placeholder return
    shapes already mirror the real leaves' contracts.
    """
    return [
        _placeholders.generate_scenario,
        _placeholders.evaluate_scenario,
        _placeholders.refine_scenario,
        _placeholders.evaluate_timing,
        _placeholders.launch_audio_render,
        content_analyst,
        visual_concepter,
        _placeholders.launch_visual_production,
        _placeholders.launch_assembly,
        _placeholders.launch_b2_sync,
        _placeholders.check_tasks,
        _placeholders.await_tasks,
        request_human_approval,
    ]


def build_demo_live_agent(
    run_dir: Path,
    *,
    topic: str,
    target_duration_sec: int,
    language: str,
) -> Any:
    """Assemble a real ``create_deep_agent`` agent for the demo run.

    The returned agent is exactly what production will run in slice 9b
    — same factory, same middleware, same approval gates — except its
    chat model is scripted and its tools are placeholders. Hand it to
    :class:`LivePipelineRun` and watch the playground emit real
    LangGraph events.

    Args:
        run_dir: Filesystem root the DeepAgent's
            :class:`FilesystemBackend` operates on. The orchestrator
            writes scratch files under here; the demo run does not
            depend on any pre-existing content.
        topic: User-supplied documentary topic (forwarded into the
            ``generate_scenario`` tool call).
        target_duration_sec: Target final video length.
        language: BCP-47 language code.

    Returns:
        A compiled LangGraph ``CompiledStateGraph``.
    """
    chat_model = _ScriptedToolCallingModel(
        responses=_demo_chat_script(topic, target_duration_sec, language),
    )
    base_tools = _demo_tools()
    real_overrides = build_real_worker_tools(run_dir)
    tools = apply_real_worker_overrides(base_tools, real_overrides)
    # langchain HITL middleware vocabulary: ``approve`` / ``edit`` /
    # ``reject``. The project's operator-console vocabulary
    # (``accept`` / ``respond`` / …) is translated at the queue
    # boundary; for the in-process demo we use the middleware's
    # native vocab so :func:`auto_accept_interrupt` can resolve every
    # gate without a translator.
    interrupt_on: dict[str, bool | dict[str, Any]] = {
        "launch_visual_production": {
            "allowed_decisions": ["approve", "edit", "reject"],
        },
        "launch_assembly": {
            "allowed_decisions": ["approve", "reject"],
        },
        "request_human_approval": {
            "allowed_decisions": ["approve", "reject"],
        },
    }
    return build_orchestrator(
        run_dir,
        model=chat_model,
        tools=tools,
        # The demo skips SubAgent delegation — the visual / production
        # SubAgents add a layer of indirection that is exercised
        # separately. The demo chat script calls
        # ``launch_visual_production`` directly so the approval gate
        # fires at the orchestrator level.
        subagents=[],
        # Empty memory: AGENTS.md exists in the repo and is read by
        # production runs, but the demo does not need it (the script
        # ignores memory anyway).
        memory=[],
        interrupt_on=interrupt_on,
        # ``Command(resume=...)`` requires a checkpointer. Each demo
        # run gets its own in-memory saver so concurrent runs do not
        # collide on the default thread id.
        checkpointer=InMemorySaver(),
    )


__all__ = ["build_demo_live_agent"]
