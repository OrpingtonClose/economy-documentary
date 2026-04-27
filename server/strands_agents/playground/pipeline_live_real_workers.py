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

import base64
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import tool

from strands_agents import _placeholders
from strands_agents._real_assembly_tools import build_real_assembly_tools
from strands_agents._real_b2_tools import build_real_b2_tools

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

# Module-level lock that serialises ``/video/render`` POSTs to a single
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
    """Write ``payload`` to ``run_dir/scene_<scene_id>-<token>.<suffix>``.

    Returns the absolute path. The token suffix is a short random
    hex so concurrent renders for the same scene do not clobber each
    other (e.g. on retries).
    """
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    path = artifacts / f"{scene_id}-{token}.{suffix}"
    path.write_bytes(payload)
    return path


def _resolve_audio_text(
    scene_id: str,
    text: str | None,
) -> str:
    """Pick the TTS narration string for ``scene_id``.

    Slice 9c contract: the orchestrator passes the scene's narration
    through ``text`` so the real-worker dispatcher renders the actual
    script. When ``text`` is missing or whitespace, fall back to the
    pre-9c placeholder line so a misconfigured caller does not silently
    render an empty WAV.
    """
    if isinstance(text, str) and text.strip():
        return text.strip()
    return (
        f"Documentary narration for scene {scene_id}. "
        "Live dispatched via the real Qwen3-TTS worker."
    )


def _build_audio_tool(*, run_dir: Path, worker_url: str) -> Any:
    """Return a ``@tool``-decorated callable that POSTs to the live TTS."""

    @tool
    def launch_audio_render(
        scene_id: str,
        voice_id: str,
        text: str | None = None,
    ) -> dict[str, Any]:
        """Real Qwen3-TTS dispatch (slice 9d-wire / 9c).

        Sends a /tts/render request to the live worker, decodes the
        base64 WAV, persists it under ``run_dir/artifacts/``, and
        returns a placeholder-shaped envelope so the orchestrator's
        scripted brain stays compatible with slice 9a. The optional
        ``text`` argument carries the scene's actual narration so the
        TTS renders the script the scenario agent produced, not a
        hard-coded placeholder line (slice 9c).
        """
        resolved_text = _resolve_audio_text(scene_id, text)
        body = {
            "scene_id": scene_id,
            "voice_id": voice_id,
            "text": resolved_text,
            "duration_s": _DEFAULT_AUDIO_DURATION_S,
            "seed": _DEFAULT_SEED,
        }
        started_ms = _now_ms()
        try:
            with httpx.Client(timeout=_DEFAULT_TIMEOUT_S) as client:
                resp = client.post(f"{worker_url}/tts/render", json=body)
        except httpx.HTTPError as exc:
            logger.warning(
                "scene_id=<%s>, error=<%r> | tts dispatch failed", scene_id, exc
            )
            return _envelope(
                "launch_audio_render",
                scene_id=scene_id,
                voice_id=voice_id,
                error=repr(exc),
            )

        elapsed_ms = _now_ms() - started_ms
        try:
            payload = resp.json() if resp.content else {}
        except ValueError:
            payload = {"_text": resp.text}

        wav_path: Path | None = None
        wav_len = 0
        if isinstance(payload, dict) and "wav_base64" in payload:
            try:
                wav_bytes = base64.b64decode(payload["wav_base64"])
                wav_len = len(wav_bytes)
                wav_path = _persist_artifact(run_dir, scene_id, "wav", wav_bytes)
                logger.info(
                    "scene_id=<%s>, bytes=<%d>, path=<%s> | wav persisted",
                    scene_id,
                    wav_len,
                    wav_path,
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "scene_id=<%s>, error=<%r> | wav decode failed",
                    scene_id,
                    exc,
                )

        duration_sec = (
            float(payload["duration_s"])
            if isinstance(payload, dict)
            and isinstance(payload.get("duration_s"), int | float)
            else None
        )
        alignment: dict[str, Any] | None = None
        if duration_sec is not None and duration_sec > 0:
            alignment = {
                "scene_id": scene_id,
                "duration_sec": duration_sec,
                "source": "qwen3-tts-engine-duration",
            }
            logger.info(
                "scene_id=<%s>, duration_sec=<%.3f> | tts alignment captured",
                scene_id,
                duration_sec,
            )

        return _envelope(
            "launch_audio_render",
            scene_id=scene_id,
            voice_id=voice_id,
            text=resolved_text,
            status_code=resp.status_code,
            wav_bytes_len=wav_len,
            wav_path=str(wav_path) if wav_path else None,
            duration_s=duration_sec,
            elapsed_ms=elapsed_ms,
            engine=payload.get("engine") if isinstance(payload, dict) else None,
            alignment=alignment,
        )

    return launch_audio_render


def _resolve_visual_prompt(
    visual_concept: Any,
    prompt: str | None,
) -> str:
    """Pick the LTX prompt string for a scene render.

    Slice 9c contract: the orchestrator either supplies a fully-formed
    ``prompt`` string (preferred) or — when only the structured
    ``visual_concept`` is available — a richer string is synthesised
    from the concept's known fields (phrases, shot_type, camera
    movement, mood, palette). Falls back to the pre-9c placeholder
    line only when both inputs are empty.
    """
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()

    if isinstance(visual_concept, dict):
        parts: list[str] = []
        phrases = visual_concept.get("phrases")
        if isinstance(phrases, list) and phrases:
            parts.append(" ".join(str(p).strip() for p in phrases if str(p).strip()))
        for key in ("shot_type", "camera_movement", "mood", "palette", "style"):
            value = visual_concept.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(f"{key.replace('_', ' ')}: {value.strip()}")
            elif isinstance(value, list):
                joined = ", ".join(str(v).strip() for v in value if str(v).strip())
                if joined:
                    parts.append(f"{key.replace('_', ' ')}: {joined}")
        synthesised = ". ".join(p for p in parts if p)
        if synthesised.strip():
            return synthesised.strip()

    return "Documentary establishing shot, slow zoom, cinematic lighting"


def _build_visual_tool(*, run_dir: Path, worker_url: str) -> Any:
    """Return a ``@tool``-decorated callable that POSTs to the live LTX worker."""

    @tool
    def launch_visual_production(
        scene_id: str,
        visual_concept: dict[str, Any],
        prompt: str | None = None,
        target_duration_s: float | None = None,
    ) -> dict[str, Any]:
        """Real LTX-2.3 BASIC dispatch (slice 9d-wire / 9c / 9k).

        Sends a /video/render request to the live worker, decodes the
        base64 MP4, persists it under ``run_dir/artifacts/``, and
        returns a placeholder-shaped envelope. The optional ``prompt``
        argument (slice 9c) carries a fully-formed style-locked LTX
        prompt so the orchestrator can drive video quality from real
        scenario content; falls back to a string synthesised from the
        ``visual_concept`` dict, then to a generic establishing-shot
        line.

        ``target_duration_s`` (slice 9k) is the per-scene narration
        length the worker should match. LTX-2.3 emits ~89 frames at
        24 fps (~3.7 s) by default; without this argument the muxer
        freezes the last frame for the remainder of the audio
        track. Passing the per-scene narration duration tells the
        worker to render ``ceil(target_duration_s * fps)`` frames so
        video and audio cover the same wall-clock window.
        """
        resolved_prompt = _resolve_visual_prompt(visual_concept, prompt)
        requested_duration = (
            float(target_duration_s)
            if isinstance(target_duration_s, int | float)
            and target_duration_s > 0
            else _DEFAULT_VIDEO_DURATION_S
        )
        resolved_duration = min(requested_duration, _MAX_VIDEO_DURATION_S)
        if resolved_duration < requested_duration:
            logger.info(
                "scene_id=<%s>, requested=<%.3f>, capped=<%.3f> | "
                "ltx duration capped to avoid VAE OOM; muxer loops clip",
                scene_id,
                requested_duration,
                resolved_duration,
            )

        body = {
            "prompt": resolved_prompt,
            "duration_s": resolved_duration,
            "fps": _DEFAULT_FPS,
            "seed": _DEFAULT_SEED,
        }
        started_ms = _now_ms()
        try:
            with _video_dispatch_lock:
                logger.info("scene_id=<%s> | ltx dispatch acquired GPU lock", scene_id)
                with httpx.Client(timeout=_DEFAULT_TIMEOUT_S) as client:
                    resp = client.post(f"{worker_url}/video/render", json=body)
        except httpx.HTTPError as exc:
            logger.warning(
                "scene_id=<%s>, error=<%r> | ltx dispatch failed",
                scene_id,
                exc,
            )
            return _envelope(
                "launch_visual_production",
                scene_id=scene_id,
                visual_concept=visual_concept,
                error=repr(exc),
            )

        elapsed_ms = _now_ms() - started_ms
        try:
            payload = resp.json() if resp.content else {}
        except ValueError:
            payload = {"_text": resp.text}

        mp4_path: Path | None = None
        mp4_len = 0
        if isinstance(payload, dict) and "mp4_base64" in payload:
            try:
                mp4_bytes = base64.b64decode(payload["mp4_base64"])
                mp4_len = len(mp4_bytes)
                mp4_path = _persist_artifact(run_dir, scene_id, "mp4", mp4_bytes)
                logger.info(
                    "scene_id=<%s>, bytes=<%d>, path=<%s> | mp4 persisted",
                    scene_id,
                    mp4_len,
                    mp4_path,
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "scene_id=<%s>, error=<%r> | mp4 decode failed",
                    scene_id,
                    exc,
                )

        return _envelope(
            "launch_visual_production",
            scene_id=scene_id,
            visual_concept=visual_concept,
            prompt=resolved_prompt,
            target_duration_s=resolved_duration,
            duration_s=resolved_duration,
            status_code=resp.status_code,
            mp4_bytes_len=mp4_len,
            mp4_path=str(mp4_path) if mp4_path else None,
            elapsed_ms=elapsed_ms,
            engine=payload.get("engine") if isinstance(payload, dict) else None,
        )

    return launch_visual_production


def build_real_worker_tools(
    run_dir: Path,
    *,
    audio_worker_url: str | None = None,
    video_worker_url: str | None = None,
    enable_real_assembly: bool | None = None,
    enable_real_b2: bool | None = None,
) -> dict[str, Any]:
    """Return ``{tool_name: tool}`` overrides for the live demo.

    Args:
        run_dir: The orchestrator's run-dir; artifact files persist
            under ``run_dir/artifacts/``.
        audio_worker_url: ``http(s)://host:port`` base URL for the
            Qwen3-TTS worker. ``None`` disables real audio dispatch
            (placeholder used).
        video_worker_url: Base URL for the LTX-Video worker. ``None``
            disables real video dispatch.
        enable_real_assembly: Optional explicit toggle for the slice
            9g real assembly overlay. ``None`` falls through to the
            ``ENABLE_REAL_ASSEMBLY`` env var.
        enable_real_b2: Optional explicit toggle for the slice 9h
            real B2 sync overlay. ``None`` falls through to the
            ``ENABLE_REAL_B2`` env var.

    Returns:
        A possibly-empty dict mapping tool names to ``@tool``-decorated
        callables. Empty when no overlay is active — caller falls back
        to the placeholder set.
    """
    overrides: dict[str, Any] = {}
    audio = (audio_worker_url or os.environ.get("QWEN3_TTS_WORKER_URL", "")).rstrip("/")
    video = (video_worker_url or os.environ.get("LTX_VIDEO_WORKER_URL", "")).rstrip("/")
    if audio:
        overrides["launch_audio_render"] = _build_audio_tool(
            run_dir=run_dir, worker_url=audio
        )
    if video:
        overrides["launch_visual_production"] = _build_visual_tool(
            run_dir=run_dir, worker_url=video
        )
    overrides.update(
        build_real_assembly_tools(run_dir=run_dir, enabled=enable_real_assembly)
    )
    overrides.update(build_real_b2_tools(run_dir=run_dir, enabled=enable_real_b2))
    if not overrides:
        return overrides
    logger.info(
        "audio=<%s>, video=<%s>, overrides=<%s> | real-worker tools built",
        bool(audio),
        bool(video),
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
_ = _placeholders
