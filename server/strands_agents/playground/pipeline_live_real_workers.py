"""Real-worker dispatch tools for the live pipeline demo (slice 9d-wire).

The slice 9a/9b ``build_demo_live_agent`` wires the real DeepAgent
orchestrator end-to-end against placeholder tools. Those placeholders
return ``{"status": "placeholder", ...}`` envelopes — the orchestrator
believes it dispatched audio + video renders, but no real bytes are
produced. That kept slice 9a's runner cost-free, but it also means
``/pipeline?mode=live`` does **not** exercise the LTX-2.3 BASIC
engine (slice 9d-wire) end-to-end.

This module supplies real-dispatch sibling tools for
``launch_audio_render`` and ``launch_visual_production`` that:

* POST to the live worker URL (``QWEN3_TTS_WORKER_URL`` /
  ``LTX_VIDEO_WORKER_URL``) with the orchestrator's args translated
  into the worker's request schema.
* Decode the worker's base64 audio/video payload and persist it under
  the orchestrator's ``run_dir`` so the per-run filesystem audit (the
  proof the user actually cares about) can find real WAV / MP4 bytes
  there.
* Return the same ``{"status", "tool", "args"}`` envelope shape the
  orchestrator's placeholder ledger uses, so the rest of the
  scripted-LLM live runner observes the same state shape it always
  did. The scripted brain ignores tool returns; only the side-effect
  matters.

The dispatcher is environment-gated: if ``LTX_VIDEO_WORKER_URL`` is
unset, ``build_real_worker_tools`` returns the placeholder set
unchanged so CI and offline test runs keep working. Audio dispatch
falls back to the placeholder per-tool so a missing TTS URL doesn't
block visual proof.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import tool  # type: ignore[import-not-found]

from strands_agents._real_assembly_tools import build_real_assembly_tools
from strands_agents._real_b2_tools import build_real_b2_tools
from strands_agents.artifact_uploader import with_b2_upload

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_S = 30 * 60.0
_DEFAULT_AUDIO_DURATION_S = 5.0
_DEFAULT_VIDEO_DURATION_S = 4.0
# LTX-2.3 22B VAE decode is O(frames) in VRAM. On a 140 GiB H200 the
# decoder OOMs above ~6 s @ 1280x704 (allocates ~128 GiB for 353 frames).
# Cap dispatched duration here; the assembly muxer already loops the
# resulting clip to fill the full narration via ``-stream_loop -1`` +
# ``-t <audio_duration>`` (see server/tools/assembly_tools.py). Looping
# preserves visible motion across the whole audio span — the slice 9j
# frozen-frame regression that the loop fix targets.
_MAX_VIDEO_DURATION_S = 5.0
_DEFAULT_FPS = 24
_DEFAULT_SEED = 7

# Module-level lock that serialises ``POST /`` video requests to a single
# H200-class GPU worker. AGENTS.md hard invariant: "Never parallelise
# launch_visual_production jobs onto the same GPU worker." When the
# orchestrator dispatches N scenes in one tool-call turn, LangGraph runs
# the @tool callables concurrently on a thread pool; without this lock,
# the worker is hammered with N simultaneous renders and OOMs on all but
# one. Acquiring this lock around the httpx POST keeps the GPU queue at
# depth=1 — renders complete sequentially but every scene returns a real
# MP4. Audio dispatch runs against a separate worker pool and is left
# parallel.
_video_dispatch_lock = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _envelope(name: str, **args: Any) -> dict[str, Any]:
    """Match the placeholder envelope shape so downstream observers stay stable."""
    return {"status": "real-worker-dispatched", "tool": name, "args": args}


def _persist_artifact(
    run_dir: Path, scene_id: str, suffix: str, payload: bytes
) -> Path:
    """Write ``payload`` to ``run_dir/artifacts/<scene_id>.<suffix>``.

    The path is deterministic: ``{run_dir}/artifacts/{scene_id}.{suffix}``.
    Per-scene QA gates downstream
    (:func:`strands_agents.qa_gates.qa_audio_completeness`,
    :func:`~strands_agents.qa_gates.qa_duration_align`,
    :func:`~strands_agents.qa_gates.qa_stills_judge`) reconstruct the
    same path from ``(artifacts_root, scene_id, suffix)``, so any token
    suffix here would silently break the QA pipeline (the gate would
    open a non-existent file and fail-by-default with no measurements).
    Each pipeline run gets its own ``run_dir`` and each scene renders
    exactly once per run, so a deterministic name cannot collide.
    """
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    path = artifacts / f"{scene_id}.{suffix}"
    path.write_bytes(payload)
    return path










def build_real_worker_tools(
    run_dir: Path,
    *,
    enable_real_assembly: bool | None = None,
    enable_real_b2: bool | None = None,
) -> dict[str, Any]:
    """Return ``{tool_name: tool}`` overrides for real assembly and B2.

    Direct audio/video worker dispatch has been removed.
    Only assembly and B2 tools remain.
    """
    overrides: dict[str, Any] = {}
    overrides.update(
        build_real_assembly_tools(run_dir=run_dir, enabled=enable_real_assembly)
    )
    overrides.update(build_real_b2_tools(run_dir=run_dir, enabled=enable_real_b2))
    if not overrides:
        return overrides
    logger.info(
        "overrides=<%s> | real tools built",
        sorted(overrides.keys()),
    )
    return overrides


def apply_real_worker_overrides(
    base_tools: list[Any],
    overrides: dict[str, Any],
) -> list[Any]:
    """Replace placeholder tools in ``base_tools`` with overrides by name.

    A real-worker tool replaces the placeholder iff their ``.name``
    attributes match. Tools without a matching override pass through
    unchanged. The list order is preserved so the orchestrator's
    tool-picking heuristics see a stable surface.
    """
    if not overrides:
        return list(base_tools)
    out: list[Any] = []
    for tool_obj in base_tools:
        name = getattr(tool_obj, "name", None)
        if name in overrides:
            out.append(overrides[name])
        else:
            out.append(tool_obj)
    return out


__all__ = [
    "apply_real_worker_overrides",
    "build_real_worker_tools",
]


# Module-level reference so static analyzers don't drop the
# placeholder import (used downstream as a fallback target).

