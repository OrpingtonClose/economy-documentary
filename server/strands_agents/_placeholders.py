"""Placeholder tools used while dependency PRs (01 – 13) are still open.

Each function here returns a structured ``placeholder`` envelope so the
orchestrator (:mod:`server.strands_agents.pipeline`) can be constructed
off ``main`` without importing leaves whose PRs have not yet merged.

Once a given per-component PR lands, the dynamic-import lookup in
:func:`server.strands_agents.pipeline.build_default_tools` picks up the
real tool automatically and the placeholder stays unreferenced. The
placeholders are intentionally small, deterministic, and credential-free
so CI stays hermetic.

All placeholder tools share the same return shape::

    {"status": "placeholder", "tool": "<name>", "args": {...}}

so downstream tests can assert a consistent shape when they fall back
to placeholders.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool


def _envelope(name: str, **args: Any) -> dict[str, Any]:
    return {"status": "placeholder", "tool": name, "args": args}


@tool
def generate_scenario(
    topic: str,
    num_scenes: int = 5,
    style: str = "documentary",
    language: str = "en",
) -> dict[str, Any]:
    """Scenario generation placeholder (real impl: component 01)."""

    return _envelope(
        "generate_scenario",
        topic=topic,
        num_scenes=num_scenes,
        style=style,
        language=language,
    )


@tool
def evaluate_scenario(
    scenes: list[dict[str, Any]],
    style_lock: dict[str, Any] | None = None,
    target_duration_sec: float = 300.0,
) -> dict[str, Any]:
    """Scenario evaluation placeholder (real impl: component 01)."""

    return _envelope(
        "evaluate_scenario",
        scenes=scenes,
        style_lock=style_lock or {},
        target_duration_sec=target_duration_sec,
    )


@tool
def refine_scenario(
    scenes: list[dict[str, Any]],
    feedback: dict[str, Any],
) -> dict[str, Any]:
    """Scenario refinement placeholder (real impl: component 03)."""

    return _envelope("refine_scenario", scenes=scenes, feedback=feedback)


@tool
def evaluate_timing(
    timeline: dict[str, Any],
    alignment: dict[str, Any],
    target_duration_sec: float,
) -> dict[str, Any]:
    """Timing evaluation placeholder (real impl: component 02)."""

    return _envelope(
        "evaluate_timing",
        timeline=timeline,
        alignment=alignment,
        target_duration_sec=target_duration_sec,
    )


@tool
def launch_audio_render(
    scene_id: str,
    voice_id: str,
    text: str | None = None,
) -> dict[str, Any]:
    """Audio render launch placeholder (real impl: component 04).

    The optional ``text`` argument carries the scene's narration so a
    real-worker overlay (slice 9d-wire / 9c) can dispatch a non-trivial
    TTS prompt instead of a hard-coded "Documentary narration for scene
    X" line. The placeholder simply echoes it in the envelope so unit
    tests can assert the orchestrator passed real content through.
    """

    return _envelope(
        "launch_audio_render",
        scene_id=scene_id,
        voice_id=voice_id,
        text=text,
    )


@tool
def launch_visual_production(
    scene_id: str,
    visual_concept: dict[str, Any],
    prompt: str | None = None,
) -> dict[str, Any]:
    """Visual production launch placeholder (real impl: component 10).

    The optional ``prompt`` argument carries a fully-formed LTX
    prompt so the real-worker overlay can dispatch a rich,
    style-locked description instead of synthesising one from the
    sparse ``visual_concept`` dict. Mirrors ``launch_audio_render``'s
    ``text`` argument (slice 9c).
    """

    return _envelope(
        "launch_visual_production",
        scene_id=scene_id,
        visual_concept=visual_concept,
        prompt=prompt,
    )


@tool
def launch_assembly(
    timeline: dict[str, Any] | None = None,
    output_path: str | None = None,
    clip_artifacts: list[dict[str, Any]] | None = None,
    target_duration_sec: float | None = None,
) -> dict[str, Any]:
    """Assembly launch placeholder (real impl: component 11).

    Slice 9g-assembly added the optional ``clip_artifacts`` argument so
    the real-worker overlay can compose a master MP4 from the per-scene
    artifacts persisted by the audio/video dispatchers. ``timeline`` /
    ``output_path`` stay accepted (and default to ``None``) for
    backward compatibility with the pre-9g demo script and any
    downstream caller still emitting the legacy shape.
    """

    return _envelope(
        "launch_assembly",
        timeline=timeline or {},
        output_path=output_path or "",
        clip_artifacts=clip_artifacts or [],
        target_duration_sec=target_duration_sec,
    )


@tool
def launch_b2_sync(
    artifact_path: str | None = None,
    master_mp4_path: str | None = None,
    clip_artifacts: list[dict[str, Any]] | None = None,
    scenario_path: str | None = None,
    run_id: str | None = None,
    revision_tag: str | None = None,
) -> dict[str, Any]:
    """B2 sync placeholder (infrastructure leaf; real impl: slice 9h).

    Slice 9h-b2-publish added the optional ``master_mp4_path`` /
    ``clip_artifacts`` / ``scenario_path`` / ``run_id`` /
    ``revision_tag`` arguments so the real-worker overlay can upload
    every artifact in the run-dir to B2 and return a manifest. The
    legacy ``artifact_path`` argument is preserved (and defaults to
    ``None``) so pre-9h callers still work — both shapes echo through
    the placeholder unchanged.
    """

    return _envelope(
        "launch_b2_sync",
        artifact_path=artifact_path or "",
        master_mp4_path=master_mp4_path,
        clip_artifacts=clip_artifacts or [],
        scenario_path=scenario_path,
        run_id=run_id,
        revision_tag=revision_tag,
    )


@tool
def check_tasks(task_ids: list[str]) -> dict[str, Any]:
    """Task-pool status placeholder (real impl: phase 1, task_pool.py)."""

    return _envelope("check_tasks", task_ids=task_ids)


@tool
def await_tasks(task_ids: list[str], timeout_sec: float = 600.0) -> dict[str, Any]:
    """Task-pool await placeholder (real impl: phase 1, task_pool.py)."""

    return _envelope(
        "await_tasks",
        task_ids=task_ids,
        timeout_sec=timeout_sec,
    )


__all__ = [
    "await_tasks",
    "check_tasks",
    "evaluate_scenario",
    "evaluate_timing",
    "generate_scenario",
    "launch_assembly",
    "launch_audio_render",
    "launch_b2_sync",
    "launch_visual_production",
    "refine_scenario",
]
