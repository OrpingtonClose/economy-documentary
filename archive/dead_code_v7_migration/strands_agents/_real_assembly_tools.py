"""Real assembly tool — slice 9g-assembly.

Composes a single master ``.mp4`` from the per-scene artifacts the
audio (Qwen3-TTS) + video (LTX-2.3) dispatchers persist to
``run_dir/artifacts/`` during a ``/pipeline?mode=live`` run.

Mirrors the architecture pattern established by
:mod:`server.strands_agents._real_scenario_tools` and
:mod:`server.strands_agents.playground.pipeline_live_real_workers`:

* Pure-Python core (:func:`compose_master_mp4`) — helper-injectable so
  unit tests don't need ffmpeg on the box. The orchestrator's
  trajectory through this module is the same regardless of whether
  ffmpeg is the real binary or a stub.
* LangChain ``@tool`` factory (:func:`make_real_assembly_tool`) — closes
  over a ``run_dir`` and the helper bag, returns a
  ``BaseTool`` whose ``.name == "launch_assembly"`` so
  :func:`apply_real_worker_overrides` can swap it in by name.
* Env-gated overlay builder (:func:`build_real_assembly_tools`) —
  empty dict means "fall back to placeholder", which is the contract
  the rest of the real-worker overlays already use.

Why a separate module instead of importing
:mod:`server.strands_agents.tools.assembly_tool`: that module's
``assemble_final_cut`` is decorated with the **Strands** ``@tool``
decorator (it produces a ``DecoratedFunctionTool`` exposing
``.tool_name``) which is incompatible with the LangChain ``BaseTool``
interface ``deepagents.create_deep_agent`` expects. Same constraint
that drove slice 9c (real LLM scenario tools), slice 9d-wire (real
audio/video dispatchers), and slice 9f-timing-real (real
``evaluate_timing``) into their own re-wrapper modules.

Gate: the override set is empty unless ``ENABLE_REAL_ASSEMBLY=1`` is
set in the environment. With the gate off, the placeholder echo
continues to fire so CI stays hermetic and credential/ffmpeg-free.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from langchain_core.tools import tool  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)


_FALLBACK_REASON = (
    "ffmpeg helpers unavailable; real assembly tool disabled (slice 9g-assembly gate)"
)


# Helper-protocol type aliases. The real implementations come from
# ``server.tools.assembly_tools``. Both helpers return a JSON string
# (the legacy contract from the ADK-era assembly tools) which this
# module parses internally — callers only see the ``compose_master_mp4``
# return shape.
MuxAudioVideoFn = Callable[[str, str, str], str]
ConcatClipsFn = Callable[[str, str], str]


def _resolve_artifact_dir(run_dir: Path) -> Path:
    """Return ``run_dir/artifacts``, creating it on demand."""
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def _resolve_one(
    artifact_dir: Path,
    scene_id: str,
    explicit: str | None,
    suffix: str,
) -> Path | None:
    """Resolve a single per-scene artifact path.

    Order of precedence:
    1. ``explicit`` (orchestrator-supplied absolute path).
    2. ``artifact_dir/{scene_id}.{suffix}`` (canonical layout).
    3. ``artifact_dir/{scene_id}-*.{suffix}`` glob — the layout
       :func:`pipeline_live_real_workers._persist_artifact` writes
       (``{scene_id}-{8hex}.{suffix}`` for retry-safety). The most
       recent file wins so a successful retry beats an earlier failure.
    """
    if explicit:
        candidate = Path(explicit)
        if candidate.exists():
            return candidate
    canonical = artifact_dir / f"{scene_id}.{suffix}"
    if canonical.exists():
        return canonical
    matches = sorted(
        artifact_dir.glob(f"{scene_id}-*.{suffix}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _resolve_clip_paths(
    artifact_dir: Path,
    scene_id: str,
    explicit_mp4: str | None,
    explicit_wav: str | None,
) -> tuple[Path | None, Path | None]:
    """Resolve per-scene mp4 / wav paths.

    Returns:
        A ``(mp4_path, wav_path)`` tuple. Either entry may be ``None``
        when the file does not exist on disk; callers must treat
        missing video as a hard error and missing audio as
        "video-only mux".
    """
    return (
        _resolve_one(artifact_dir, scene_id, explicit_mp4, "mp4"),
        _resolve_one(artifact_dir, scene_id, explicit_wav, "wav"),
    )


def _parse_helper_payload(raw: Any) -> dict[str, Any]:
    """Parse the JSON envelope ``mux_audio_video`` / ``concat_clips`` return.

    The shared ``server.tools.assembly_tools`` helpers historically
    return a JSON string for ADK compatibility. Stub helpers in tests
    may return a plain dict to skip the round-trip — this function
    accepts both.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("error=<%r> | helper returned non-JSON payload", exc)
            return {"error": f"helper returned non-JSON: {raw[:200]}"}
        if isinstance(payload, dict):
            return payload
        return {"error": f"helper returned non-dict JSON: {payload}"}
    return {"error": f"helper returned unexpected type: {type(raw).__name__}"}


def compose_master_mp4(
    clip_artifacts: list[dict[str, Any]],
    output_dir: Path,
    *,
    mux_audio_video_helper: MuxAudioVideoFn,
    concat_clips_helper: ConcatClipsFn,
) -> dict[str, Any]:
    """Compose a single master MP4 from per-scene clip artifacts.

    Two-stage ffmpeg pipeline:
    1. Per-scene mux: each scene's video is muxed with its audio (when
       a ``.wav`` is present alongside the ``.mp4``) into
       ``output_dir/{scene_id}.muxed.mp4``. Scenes without audio pass
       through to concat unchanged (the source video file is added to
       the concat list directly).
    2. Concat: every muxed (or pass-through) clip is concatenated by
       ``ffmpeg``'s concat demuxer into ``output_dir/master.mp4``.

    Args:
        clip_artifacts: One entry per scene. Each must carry a
            ``scene_id`` (``str``) and may carry explicit
            ``mp4_path`` / ``wav_path`` (``str``) and ``duration_sec``
            (``float``). Missing paths are resolved relative to
            ``output_dir`` by ``{scene_id}.mp4`` / ``{scene_id}.wav``.
        output_dir: Directory to write per-scene muxed clips and the
            final ``master.mp4`` into. Created on demand.
        mux_audio_video_helper: Callable
            ``(audio_path, video_path, output_path) -> json_payload``
            mirroring :func:`server.tools.assembly_tools.mux_audio_video`.
            Tests inject stubs.
        concat_clips_helper: Callable
            ``(comma_separated_paths, output_path) -> json_payload``
            mirroring :func:`server.tools.assembly_tools.concat_clips`.
            Tests inject stubs.

    Returns:
        ``{"master_mp4_path": str, "scene_count": int, "muxed_count":
        int, "concat_inputs": list[str], "duration_sec_estimate":
        float}`` on success.

    Raises:
        ValueError: When ``clip_artifacts`` is empty, when a scene
            entry is malformed, when a scene's video file cannot be
            located, or when a helper returns an error envelope.
    """
    if not clip_artifacts:
        raise ValueError("clip_artifacts is empty; nothing to assemble")
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = output_dir
    concat_inputs: list[Path] = []
    muxed_count = 0
    duration_total = 0.0
    for entry in clip_artifacts:
        if not isinstance(entry, dict):
            raise ValueError(f"clip_artifacts entry is not a dict: {entry!r}")
        scene_id = entry.get("scene_id")
        if not isinstance(scene_id, str) or not scene_id.strip():
            raise ValueError(f"clip_artifacts entry missing scene_id: {entry!r}")
        mp4_path, wav_path = _resolve_clip_paths(
            artifact_dir,
            scene_id,
            entry.get("mp4_path") if isinstance(entry.get("mp4_path"), str) else None,
            entry.get("wav_path") if isinstance(entry.get("wav_path"), str) else None,
        )
        if mp4_path is None:
            raise ValueError(
                f"scene_id=<{scene_id}> has no resolvable mp4_path; "
                f"checked entry.mp4_path and {artifact_dir}/{scene_id}.mp4"
            )
        try:
            duration_total += float(entry.get("duration_sec") or 0.0)
        except (TypeError, ValueError):
            pass
        if wav_path is None:
            logger.info(
                "scene_id=<%s>, mp4=<%s> | no audio — pass-through",
                scene_id,
                mp4_path,
            )
            concat_inputs.append(mp4_path)
            continue
        muxed_path = output_dir / f"{scene_id}.muxed.mp4"
        raw = mux_audio_video_helper(str(wav_path), str(mp4_path), str(muxed_path))
        payload = _parse_helper_payload(raw)
        if "error" in payload:
            raise ValueError(f"scene_id=<{scene_id}> mux failed: {payload['error']}")
        muxed_count += 1
        logger.info(
            "scene_id=<%s>, mp4=<%s>, wav=<%s>, muxed=<%s> | mux ok",
            scene_id,
            mp4_path,
            wav_path,
            muxed_path,
        )
        concat_inputs.append(muxed_path)
    master_path = output_dir / "master.mp4"
    raw_concat = concat_clips_helper(
        ",".join(str(p) for p in concat_inputs),
        str(master_path),
    )
    concat_payload = _parse_helper_payload(raw_concat)
    if "error" in concat_payload:
        raise ValueError(f"concat failed: {concat_payload['error']}")
    logger.info(
        "scene_count=<%d>, muxed=<%d>, master=<%s>, duration=<%.2f> "
        "| master mp4 composed",
        len(clip_artifacts),
        muxed_count,
        master_path,
        duration_total,
    )
    return {
        "master_mp4_path": str(master_path),
        "scene_count": len(clip_artifacts),
        "muxed_count": muxed_count,
        "concat_inputs": [str(p) for p in concat_inputs],
        "duration_sec_estimate": duration_total,
    }


def _envelope(name: str, **args: Any) -> dict[str, Any]:
    """Match the placeholder envelope shape so downstream stays uniform."""
    return {"status": "ok", "tool": name, "engine": "ffmpeg", "args": args}


def _default_helpers() -> tuple[MuxAudioVideoFn, ConcatClipsFn]:
    """Return the production ``server.tools.assembly_tools`` helpers.

    Imported lazily so unit tests that don't touch the real overlay
    don't pull in the ADK-era assembly stack. Raises ``ImportError``
    when the helpers are unavailable — :func:`build_real_assembly_tools`
    catches that and returns an empty override set.
    """
    from tools.assembly_tools import concat_clips, mux_audio_video  # type: ignore[attr-defined]

    return cast(tuple[MuxAudioVideoFn, ConcatClipsFn], (mux_audio_video, concat_clips))


def make_real_assembly_tool(
    run_dir: Path,
    *,
    mux_audio_video_helper: MuxAudioVideoFn | None = None,
    concat_clips_helper: ConcatClipsFn | None = None,
) -> Any:
    """Return a LangChain ``@tool`` ``launch_assembly`` bound to ``run_dir``.

    The returned tool's ``.name`` is ``"launch_assembly"`` so
    :func:`apply_real_worker_overrides` swaps it in for the
    placeholder by name. The tool's args mirror the slice-9g
    placeholder signature exactly so the demo's scripted ``AIMessage``
    works against either tool unchanged.

    Args:
        run_dir: The orchestrator's run-dir; per-scene mp4/wav files
            live under ``run_dir/artifacts/`` (the same location the
            audio + video dispatchers persist to). The composed
            ``master.mp4`` is also written there.
        mux_audio_video_helper: Optional override for the ffmpeg mux
            helper. Defaults to
            :func:`server.tools.assembly_tools.mux_audio_video`.
        concat_clips_helper: Optional override for the ffmpeg concat
            helper. Defaults to
            :func:`server.tools.assembly_tools.concat_clips`.
    """
    mux_helper = mux_audio_video_helper
    concat_helper = concat_clips_helper
    if mux_helper is None or concat_helper is None:
        default_mux, default_concat = _default_helpers()
        if mux_helper is None:
            mux_helper = default_mux
        if concat_helper is None:
            concat_helper = default_concat

    @tool
    def launch_assembly(
        timeline: dict[str, Any] | None = None,
        output_path: str | None = None,
        clip_artifacts: list[dict[str, Any]] | None = None,
        target_duration_sec: float | None = None,
    ) -> dict[str, Any]:
        """Compose a master MP4 from per-scene artifacts (slice 9g-assembly).

        Args:
            timeline: Legacy pre-9g argument; ignored by the real
                tool but accepted so the demo's scripted ``AIMessage``
                can stay backward-compatible while we land follow-up
                slices.
            output_path: Legacy pre-9g argument; ignored.
            clip_artifacts: One entry per scene. Each must carry a
                ``scene_id`` and may carry explicit ``mp4_path`` /
                ``wav_path``. When the explicit paths are missing the
                tool falls back to ``run_dir/artifacts/{scene_id}.mp4``
                / ``run_dir/artifacts/{scene_id}.wav``.
            target_duration_sec: Optional movie-wide target duration
                (informational; surfaced in the envelope so the
                trajectory carries it through).

        Returns:
            ``{"status": "ok", "tool": "launch_assembly",
            "engine": "ffmpeg", "args": {scene_count, master_mp4_path,
            duration_sec_estimate, ...}}``.
        """
        artifact_dir = _resolve_artifact_dir(run_dir)
        clip_artifacts_resolved = clip_artifacts or []
        if not clip_artifacts_resolved:
            logger.warning(
                "clip_artifacts=<empty> | nothing to assemble; "
                "falling back to placeholder envelope"
            )
            return _envelope(
                "launch_assembly",
                timeline=timeline or {},
                output_path=output_path or "",
                clip_artifacts=[],
                target_duration_sec=target_duration_sec,
                scene_count=0,
                master_mp4_path=None,
                error="clip_artifacts is empty",
            )
        try:
            result = compose_master_mp4(
                clip_artifacts_resolved,
                artifact_dir,
                mux_audio_video_helper=mux_helper,
                concat_clips_helper=concat_helper,
            )
        except ValueError as exc:
            logger.warning(
                "error=<%r> | real assembly failed; surfacing in envelope",
                exc,
            )
            return _envelope(
                "launch_assembly",
                timeline=timeline or {},
                output_path=output_path or "",
                clip_artifacts=clip_artifacts_resolved,
                target_duration_sec=target_duration_sec,
                scene_count=len(clip_artifacts_resolved),
                master_mp4_path=None,
                error=str(exc),
            )
        return _envelope(
            "launch_assembly",
            timeline=timeline or {},
            output_path=output_path or "",
            clip_artifacts=clip_artifacts_resolved,
            target_duration_sec=target_duration_sec,
            scene_count=result["scene_count"],
            muxed_count=result["muxed_count"],
            master_mp4_path=result["master_mp4_path"],
            duration_sec_estimate=result["duration_sec_estimate"],
            concat_inputs=result["concat_inputs"],
        )

    return launch_assembly


def _resolve_enabled_flag(enabled: bool | None) -> bool:
    """Return whether the real assembly overlay is enabled.

    Order of precedence:
    1. Explicit ``enabled`` argument.
    2. ``ENABLE_REAL_ASSEMBLY`` env var (``1`` / ``true`` / ``yes``).
    3. Disabled (returns ``False``).
    """
    if enabled is not None:
        return bool(enabled)
    raw = os.environ.get("ENABLE_REAL_ASSEMBLY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def build_real_assembly_tools(
    *,
    run_dir: Path,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Return ``{tool_name: tool}`` overrides for the real assembly tool.

    Empty dict means "fall back to placeholder" — the contract every
    other real-worker overlay shares. Caller passes the result through
    :func:`apply_real_assembly_overrides` (or the audio/video overlay's
    by-name swap) to install the real tool.

    Args:
        run_dir: The orchestrator's run-dir; persists per-scene
            artifacts and the composed master under ``run_dir/artifacts/``.
        enabled: Optional explicit toggle. ``None`` falls through to
            the ``ENABLE_REAL_ASSEMBLY`` env var.

    Returns:
        Possibly-empty dict mapping ``"launch_assembly"`` to a
        LangChain ``@tool`` callable. Empty when the gate is off or
        when ``server.tools.assembly_tools`` cannot be imported.
    """
    if not _resolve_enabled_flag(enabled):
        logger.debug("enabled=<false> | real assembly overlay disabled")
        return {}
    try:
        tool_obj = make_real_assembly_tool(run_dir=run_dir)
    except ImportError as exc:
        logger.warning("error=<%r> | %s", exc, _FALLBACK_REASON)
        return {}
    overrides = {"launch_assembly": tool_obj}
    logger.info(
        "run_dir=<%s>, overrides=<%s> | real assembly tool built",
        run_dir,
        sorted(overrides.keys()),
    )
    return overrides


__all__ = [
    "build_real_assembly_tools",
    "compose_master_mp4",
    "make_real_assembly_tool",
]
