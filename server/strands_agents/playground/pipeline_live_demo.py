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
from strands_agents.qa_gates import (
    qa_audio_completeness,
    qa_duration_align,
    qa_stills_judge,
    qa_video_artifact_probe,
)
from strands_agents.timing_tool import compute_timing_report

logger = logging.getLogger(__name__)


# Slice 9f-timing-real: ``timing_tool.evaluate_timing`` is decorated with
# the **Strands** ``@tool`` (see ``server/strands_agents/timing_tool.py``),
# which produces a ``DecoratedFunctionTool`` incompatible with the
# LangChain ``BaseTool`` interface that ``deepagents.create_deep_agent``
# expects. We re-wrap the pure-Python core (``compute_timing_report``)
# in a LangChain ``@tool`` so the demo's tool list stays homogeneous —
# same pattern as :mod:`strands_agents._real_scenario_tools`.
@tool
def evaluate_timing(
    scenes: list[dict[str, Any]],
    whisperx_alignment: dict[str, Any],
    target_duration_sec: float,
    intent_target_sec: float | None = None,
) -> dict[str, Any]:
    """Compare narration durations against the documentary target.

    Thin LangChain wrapper around
    :func:`strands_agents.timing_tool.compute_timing_report` — kept in
    lock-step with the Strands-decorated tool of the same name so the
    orchestrator's timing loop sees identical semantics regardless of
    which dispatch path it goes through. See ``timing_tool``'s module
    docstring for the dual-tolerance schema.

    Args:
        scenes: Scene objects carrying ``voices[].text`` and per-scene
            targets (``target_duration_sec`` / ``duration_sec``).
        whisperx_alignment: WhisperX output with ``total_duration_sec``
            and ``per_scene``.
        target_duration_sec: Legacy target (from brief / blackboard).
        intent_target_sec: Typed ``BriefIntent.duration_sec`` if
            available; switches to the ±2 s absolute tolerance path
            when set and positive.

    Returns:
        ``{"timing_passed": bool, "timing_report": {...}}`` — see
        :func:`compute_timing_report` for the full report shape.
    """
    return compute_timing_report(
        scenes=scenes,
        whisperx_alignment=whisperx_alignment,
        target_duration_sec=target_duration_sec,
        intent_target_sec=intent_target_sec,
    )


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


def _ai_tool_calls_batch(
    items: Sequence[tuple[str, dict[str, Any]]],
) -> AIMessage:
    """One AIMessage carrying ``len(items)`` parallel ``tool_calls``.

    LangGraph's ``ToolNode`` dispatches every entry in ``tool_calls``
    concurrently, so this is the scripted-LLM equivalent of an
    orchestrator that emits all per-scene launches on a single turn
    (the shape AGENTS.md "Timing stage" requires for
    ``launch_audio_render`` / ``launch_visual_production``).
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
            for name, args in items
        ],
    )


def _ai_final(content: str) -> AIMessage:
    """Stop-the-loop AIMessage. No ``tool_calls`` → agent terminates."""
    return AIMessage(content=content)


# Soft cap on how many scenes the scripted demo dispatches. The cap
# protects CI-friendliness (one HITL gate per visual production fires
# each iteration through the runner) while still proving the
# multi-scene shape end-to-end.
_DEMO_MIN_SCENES = 1
_DEMO_MAX_SCENES = 6
_DEMO_SECONDS_PER_SCENE = 12


def _resolve_num_scenes(
    target_duration_sec: int,
    num_scenes: int | None,
) -> int:
    """Resolve scene count for the scripted demo.

    Precedence:
    1. Explicit ``num_scenes`` (clamped to ``[1, 6]``).
    2. ``target_duration_sec / 12``, rounded up to at least 1.
    """
    if num_scenes is not None:
        return max(_DEMO_MIN_SCENES, min(int(num_scenes), _DEMO_MAX_SCENES))
    if target_duration_sec <= 0:
        return _DEMO_MIN_SCENES
    derived = (target_duration_sec + _DEMO_SECONDS_PER_SCENE - 1) // (
        _DEMO_SECONDS_PER_SCENE
    )
    return max(_DEMO_MIN_SCENES, min(derived, _DEMO_MAX_SCENES))


def _build_scene_payload(
    scene_index: int,
    topic: str,
    duration_sec: float,
    num_scenes: int,
) -> dict[str, Any]:
    """Per-scene fixture: id, narration, visual concept, LTX prompt.

    All fields are deterministic functions of ``scene_index`` /
    ``num_scenes`` so the scripted run is reproducible across CI runs
    and the per-scene dispatch shape is easy to assert in tests.

    The 1-based ``scene_index`` is labelled ``opening`` for the first
    scene, ``closing`` for the last (when ``num_scenes >= 2``), and
    ``beat N`` for everything in between. A single-scene run keeps the
    ``opening`` label so n=1 stays a single coherent beat.
    """
    scene_id = f"scene_{scene_index:03d}"
    if scene_index == 1:
        beat_label = "opening"
    elif scene_index == num_scenes:
        beat_label = "closing"
    else:
        beat_label = f"beat {scene_index}"
    narration_text = (
        f"In {beat_label} we examine {topic}. "
        f"Across roughly {duration_sec:.0f} seconds we trace how it "
        "shapes everyday life, why it matters now, and what it tells "
        "us about the road ahead."
    )
    visual_concept = {
        "shot_count": 1,
        "style": "documentary",
        "shot_type": "establishing wide",
        "camera_movement": "slow dolly in",
        "mood": "grounded, authoritative",
        "palette": "muted earth tones",
        "phrases": [
            f"Cinematic documentary {beat_label} shot exploring {topic}",
            "soft natural light, archival texture, restrained colour grading",
        ],
    }
    visual_prompt = (
        f"Cinematic documentary {beat_label} shot exploring {topic}. "
        "Slow dolly in on a grounded subject, soft natural light, "
        "muted earth-tone palette, archival texture, restrained "
        "colour grading. 24fps, 1280x704."
    )
    return {
        "scene_id": scene_id,
        "narration_text": narration_text,
        "visual_concept": visual_concept,
        "visual_prompt": visual_prompt,
        "duration_sec": duration_sec,
    }


def _demo_chat_script(
    topic: str,
    target_duration_sec: int,
    language: str,
    num_scenes: int | None = None,
    artifacts_dir: Path | None = None,
) -> list[AIMessage]:
    """Build the deterministic chat script for the happy-path run.

    The script walks scenario → audio → visual → production →
    assembly → b2_sync and fires every approval gate. With ``N`` =
    :func:`_resolve_num_scenes`:

    - One ``launch_audio_render`` AIMessage carrying ``N`` parallel
      ``tool_calls`` (one per scene), matching the AGENTS.md
      "Timing stage" "batch launches" rule.
    - One ``launch_visual_production`` AIMessage carrying ``N``
      parallel ``tool_calls``. The HITL middleware fires the
      visual approval gate ``N`` times — the live runner's
      interrupt loop already handles per-call gating without any
      extra plumbing.
    - Per-scene serial calls for the visual SubAgent stubs
      (``content_analyst`` / ``visual_concepter``) so each scene's
      visual stage opens and closes on the stage ribbon.

    Ends with a final-answer AIMessage quoting the master MP4 URL
    so :func:`LivePipelineRun._scrape_final_mp4_url` can recover it.
    """
    n = _resolve_num_scenes(target_duration_sec, num_scenes)
    per_scene_duration = float(target_duration_sec) / float(n)
    scenes = [
        _build_scene_payload(i + 1, topic, per_scene_duration, num_scenes=n)
        for i in range(n)
    ]
    scene_ids = [s["scene_id"] for s in scenes]
    final_mp4_url = (
        f"b2://documentary/{topic.replace(' ', '_') or 'documentary'}/"
        f"master_mp4/r0001.mp4"
    )
    timeline_payload = {"scenes": scene_ids}
    scenes_payload = [
        {"id": s["scene_id"], "duration_sec": s["duration_sec"]} for s in scenes
    ]
    # Slice 9f-timing-real: scenes_for_timing matches the shape
    # ``timing_tool.evaluate_timing`` (component 02) consumes — every
    # scene carries ``scene_id`` + ``duration_sec`` + an empty
    # ``voices`` list (no inter-voice gap overhead in scripted demo).
    # whisperx_alignment_payload mirrors what a real
    # ``launch_audio_render`` dispatch surfaces: the actual rendered
    # narration duration per scene plus the movie-wide
    # ``total_duration_sec`` sum. The scripted demo seeds these from
    # ``target_duration_sec`` so the timing loop short-circuits to
    # ``timing_passed=True`` on the first pass — a real run swaps in
    # the per-scene alignment from the TTS engine.
    scenes_for_timing = [
        {
            "scene_id": s["scene_id"],
            "duration_sec": s["duration_sec"],
            "voices": [],
        }
        for s in scenes
    ]
    whisperx_alignment_payload = {
        "total_duration_sec": float(sum(s["duration_sec"] for s in scenes)),
        "per_scene": [
            {
                "scene_id": s["scene_id"],
                "duration_sec": s["duration_sec"],
            }
            for s in scenes
        ],
    }

    # Slice 9p: hoisted above script construction so the
    # ``qa_audio_completeness`` batch (emitted right after
    # ``launch_audio_render``) can reference per-scene ``audio_path``
    # values. The per-scene QA-after-visual-production loop further
    # down reuses the same ``artifacts_root``.
    artifacts_root = artifacts_dir if artifacts_dir is not None else Path(
        "artifacts"
    )

    script: list[AIMessage] = [
        _ai_tool_call(
            "generate_scenario",
            {
                "topic": topic,
                "num_scenes": n,
                "style": "documentary",
                "language": language,
            },
        ),
        _ai_tool_call(
            "evaluate_scenario",
            {
                "scenes": scenes_payload,
                "target_duration_sec": float(target_duration_sec),
            },
        ),
        # All ``launch_audio_render`` calls dispatched in parallel on a
        # single turn — AGENTS.md Timing stage "batch launches".
        _ai_tool_calls_batch(
            [
                (
                    "launch_audio_render",
                    {
                        "scene_id": s["scene_id"],
                        "voice_id": "Ryan",
                        "text": s["narration_text"],
                    },
                )
                for s in scenes
            ]
        ),
        # Slice 9p — fire ``qa_audio_completeness`` immediately after
        # every audio render returns. AGENTS.md hard invariant §5
        # "QA immediately after each artifact" + §3 "fail closed on
        # TTS". Auditory signature only (silencedetect + astats); no
        # transcription, no LLM. Catches the Qwen3-TTS abrupt-cut
        # failure mode the slice 9j run shipped with.
        _ai_tool_calls_batch(
            [
                (
                    "qa_audio_completeness",
                    {
                        "scene_id": s["scene_id"],
                        "audio_path": str(
                            artifacts_root / f"{s['scene_id']}.wav"
                        ),
                    },
                )
                for s in scenes
            ]
        ),
        _ai_tool_call(
            "evaluate_timing",
            {
                # Slice 9f-timing-real: pass the real ``scenes`` /
                # ``whisperx_alignment`` shape the production timing
                # tool consumes. The scripted demo doesn't render
                # actual audio, so per-scene durations are seeded from
                # ``target_duration_sec`` — the alignment is the
                # ground truth a real run would carry forward from
                # ``launch_audio_render``'s ``alignment`` envelope.
                "scenes": scenes_for_timing,
                "whisperx_alignment": whisperx_alignment_payload,
                "target_duration_sec": float(target_duration_sec),
            },
        ),
    ]

    # Per-scene visual analysis — serial so each scene's visual stage
    # bracket opens / closes distinctly on the UI's stage ribbon.
    for s in scenes:
        script.append(
            _ai_tool_call(
                "content_analyst",
                {
                    "scene_id": s["scene_id"],
                    "timeline_excerpt": timeline_payload,
                },
            )
        )
        script.append(
            _ai_tool_call(
                "visual_concepter",
                {
                    "scene_id": s["scene_id"],
                    "phrases": ["documentary establishing shot"],
                },
            )
        )

    # All ``launch_visual_production`` calls dispatched in parallel on a
    # single turn. The HITL middleware fires the visual gate ``N`` times.
    # Slice 9k: pass per-scene narration duration as ``target_duration_s``
    # so LTX-2.3 renders enough frames to cover the audio (default
    # ~89 frames @ 24 fps freezes after ~3.7 s otherwise).
    script.append(
        _ai_tool_calls_batch(
            [
                (
                    "launch_visual_production",
                    {
                        "scene_id": s["scene_id"],
                        "visual_concept": s["visual_concept"],
                        "prompt": s["visual_prompt"],
                        "target_duration_s": float(
                            s.get("duration_sec", per_scene_duration)
                        ),
                    },
                )
                for s in scenes
            ]
        )
    )

    # Slice 9n / 9o: AGENTS.md hard invariant §5 — “QA immediately after
    # each artifact, never batch QA at the end.” The slice 9j frozen-frame
    # regression slipped past 12 plumbing PASS checks because the live
    # scripted brain never invoked the QA gates that exist as production
    # tools in :mod:`strands_agents.qa_gates`. Wire all three gates
    # (``qa_video_artifact_probe`` / ``qa_duration_align`` /
    # ``qa_stills_judge``) on a single batched turn per scene right after
    # ``launch_visual_production``, so a future regression of the same
    # shape leaves a fail-closed verdict in the trajectory before
    # ``launch_assembly`` ever fires. The dispatchers persist artifacts
    # under ``run_dir/artifacts/<scene_id>.{mp4,wav}`` — those are the
    # paths the QA gates read. ``artifacts_root`` was hoisted above
    # script construction (slice 9p) so the post-audio QA batch can
    # reference the same per-scene paths.
    qa_calls: list[tuple[str, dict[str, Any]]] = []
    for s in scenes:
        scene_id = s["scene_id"]
        video_path = str(artifacts_root / f"{scene_id}.mp4")
        audio_path = str(artifacts_root / f"{scene_id}.wav")
        qa_calls.extend(
            [
                (
                    "qa_video_artifact_probe",
                    {"scene_id": scene_id, "video_path": video_path},
                ),
                (
                    "qa_duration_align",
                    {
                        "scene_id": scene_id,
                        "audio_path": audio_path,
                        "video_path": video_path,
                    },
                ),
                (
                    "qa_stills_judge",
                    {"scene_id": scene_id, "video_path": video_path},
                ),
            ]
        )
    if qa_calls:
        script.append(_ai_tool_calls_batch(qa_calls))

    # Slice 9g-assembly: pass per-scene ``clip_artifacts`` so the real
    # assembly overlay (env-gated on ``ENABLE_REAL_ASSEMBLY=1``) can
    # locate the per-scene mp4/wav files the audio/video dispatchers
    # persisted under ``run_dir/artifacts/``. Each entry carries the
    # ``scene_id`` and the scripted ``duration_sec`` so the envelope
    # carries a movie-wide duration estimate forward. ``timeline`` /
    # ``output_path`` stay on the call for backward compat with the
    # placeholder echo.
    clip_artifacts_payload = [
        {
            "scene_id": s["scene_id"],
            "duration_sec": s["duration_sec"],
        }
        for s in scenes
    ]
    # Slice 9h-b2-publish: pass the same ``clip_artifacts`` plus the
    # master mp4 path into ``launch_b2_sync`` so the real-worker
    # overlay (env-gated on ``ENABLE_REAL_B2=1``) can checkpoint every
    # artifact — scenario JSON, per-scene wav + mp4, master mp4 —
    # to B2 and return a manifest. ``artifact_path`` stays on the
    # call for backward compat with the placeholder echo (pre-9h
    # callers only had this single string).
    script.extend(
        [
            _ai_tool_call(
                "launch_assembly",
                {
                    "timeline": timeline_payload,
                    "output_path": final_mp4_url,
                    "clip_artifacts": clip_artifacts_payload,
                    "target_duration_sec": float(target_duration_sec),
                },
            ),
            _ai_tool_call(
                "launch_b2_sync",
                {
                    "artifact_path": final_mp4_url,
                    "master_mp4_path": final_mp4_url,
                    "clip_artifacts": clip_artifacts_payload,
                    "revision_tag": "r0001",
                },
            ),
            _ai_final(f"Final master MP4: {final_mp4_url}"),
        ]
    )
    return script


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
        # Slice 9f-timing-real: real ``evaluate_timing`` from
        # :mod:`strands_agents.timing_tool` (component 02). Consumes a
        # WhisperX-shaped ``whisperx_alignment`` payload and computes
        # ``timing_passed`` against the scene-sum / intent target with
        # the dual-tolerance schema documented there. The placeholder
        # the demo previously bound just echoed args.
        evaluate_timing,
        _placeholders.launch_audio_render,
        qa_audio_completeness,
        content_analyst,
        visual_concepter,
        _placeholders.launch_visual_production,
        # Slice 9o: AGENTS.md §5 — the production scripted brain calls
        # all three QA gates after every visual production so the
        # gates that exist in :mod:`strands_agents.qa_gates` are wired
        # into the live trajectory, not just the eval framework.
        qa_video_artifact_probe,
        qa_duration_align,
        qa_stills_judge,
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
    num_scenes: int | None = None,
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
        responses=_demo_chat_script(
            topic,
            target_duration_sec,
            language,
            num_scenes=num_scenes,
            artifacts_dir=run_dir / "artifacts",
        ),
    )
    base_tools = _demo_tools()
    real_overrides = build_real_worker_tools(
        run_dir,
        enable_real_assembly=True,
        enable_real_b2=True,
    )
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
