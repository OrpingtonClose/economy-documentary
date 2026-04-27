"""Real B2 sync tool — slice 9h-b2-publish.

Uploads every artifact in a ``run_dir`` (per-scene wav + mp4, scenario
JSON, master mp4) to the B2 checkpoint store and returns a manifest
the orchestrator can hand back to the user. Honours AGENTS.md hard
invariant 6 — *every artifact to B2 immediately* — at the slice
boundary instead of relying on per-stage callers to remember.

Mirrors the architecture pattern established by slice 9g-assembly
(:mod:`strands_agents._real_assembly_tools`):

* Pure-Python core (:func:`sync_run_artifacts`) — store-injectable so
  unit tests run against :class:`InMemoryB2CheckpointStore` and
  production runs against :class:`LiveB2CheckpointStore`. The
  orchestrator's trajectory through this module is identical for both.
* LangChain ``@tool`` factory (:func:`make_real_b2_sync_tool`) —
  closes over ``run_dir`` + the store, returns a ``BaseTool`` whose
  ``.name == "launch_b2_sync"`` so ``apply_real_worker_overrides``
  can swap it in by name.
* Env-gated overlay builder (:func:`build_real_b2_tools`) — empty
  dict means "fall back to placeholder echo", same contract every
  other slice-9 overlay uses.

Gate: the override set is empty unless ``ENABLE_REAL_B2=1`` is set.
With the gate off the placeholder fires and CI stays hermetic.

When the gate is on, the store backend is selected by ``B2_BACKEND``:

* ``B2_BACKEND=memory`` (default when gate is on) — uses
  :class:`InMemoryB2CheckpointStore`. Honest about being in-process,
  zero credentials needed, suitable for CI smoke + the
  ``/pipeline?mode=live`` demo.
* ``B2_BACKEND=live`` — uses :class:`LiveB2CheckpointStore`, which
  reads ``B2_KEY_ID`` and ``B2_APPLICATION_KEY`` from the environment.
  Slice 9j is where this gets exercised end-to-end.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from strands_agents.b2_checkpoint import (
    B2CheckpointStore,
    InMemoryB2CheckpointStore,
    checkpoint_artifact,
)
from strands_agents.b2_checkpoint.manifest import ArtifactKind

logger = logging.getLogger(__name__)


_FALLBACK_REASON = "B2 store unavailable; real b2_sync tool disabled (slice 9h gate)"


def _resolve_artifact_dir(run_dir: Path) -> Path:
    """Return ``run_dir/artifacts``, creating it on demand."""
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def _resolve_clip_paths(
    artifact_dir: Path,
    scene_id: str,
    explicit_mp4: str | None,
    explicit_wav: str | None,
) -> tuple[Path | None, Path | None]:
    """Resolve per-scene mp4 + wav paths.

    Same three-tier resolution as
    :func:`_real_assembly_tools._resolve_one`: explicit absolute path
    wins, else the canonical ``{scene_id}.{suffix}`` layout, else the
    dispatcher's retry-safe ``{scene_id}-{8hex}.{suffix}`` glob (most
    recent file wins on mtime so successful retries beat earlier
    failures).
    """

    def _resolve(explicit: str | None, suffix: str) -> Path | None:
        if explicit:
            p = Path(explicit)
            if p.is_file():
                return p
        canonical = artifact_dir / f"{scene_id}.{suffix}"
        if canonical.is_file():
            return canonical
        candidates = sorted(
            artifact_dir.glob(f"{scene_id}-*.{suffix}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    return _resolve(explicit_mp4, "mp4"), _resolve(explicit_wav, "wav")


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    """Reduce a :class:`ManifestEntry` to a JSON-friendly dict."""
    return {
        "artifact_id": entry.artifact_id,
        "run_id": entry.run_id,
        "revision_tag": entry.revision_tag,
        "kind": entry.kind,
        "b2_key": entry.b2_key,
        "sha256": entry.sha256,
        "size_bytes": entry.size_bytes,
        "uploaded_at_iso": entry.uploaded_at_iso,
    }


def sync_run_artifacts(
    *,
    run_dir: Path,
    run_id: str,
    revision_tag: str,
    store: B2CheckpointStore,
    master_mp4_path: str | None = None,
    clip_artifacts: list[dict[str, Any]] | None = None,
    scenario_path: str | None = None,
) -> dict[str, Any]:
    """Upload run artifacts to ``store`` and return a manifest dict.

    Order is fixed: scenario JSON first (so the run is identifiable
    even if a later upload fails), then each scene's wav + mp4 in the
    order they appear in ``clip_artifacts``, then the master mp4 last
    (so it's the latest entry in the ledger and easy to find).

    The function is fail-soft on missing artifacts — a missing wav
    for a scene that has audio explicitly set to ``None`` is fine,
    it's just skipped. A missing master mp4 when ``master_mp4_path``
    was passed *is* an error (the orchestrator promised one).

    Args:
        run_dir: Filesystem root of the run; ``artifacts/`` lives
            beneath this.
        run_id: The orchestrator's run id; becomes the manifest key.
        revision_tag: The preference-ledger revision active when the
            assembly completed. Must be monotonic per run.
        store: A :class:`B2CheckpointStore` to upload through.
        master_mp4_path: Absolute path to the slice-9g master mp4.
            ``None`` skips that upload (legitimate before assembly
            runs, e.g. if a caller wants to checkpoint scenario JSON
            mid-flight).
        clip_artifacts: List of ``{"scene_id": str, "mp4_path"?:
            str, "wav_path"?: str}`` entries. Paths are optional —
            when absent, the resolver falls back to the canonical /
            glob layouts under ``run_dir/artifacts/``.
        scenario_path: Absolute path to the scenario JSON file.
            ``None`` skips that upload.

    Returns:
        ``{"manifest": [...], "uploaded_count": int,
        "kinds": {"scene_json": int, "audio_wav": int, ...},
        "run_id": str, "revision_tag": str}``.
    """
    artifact_dir = _resolve_artifact_dir(run_dir)
    uploaded: list[dict[str, Any]] = []
    kind_counts: dict[str, int] = {}

    def _upload(path: Path, kind: ArtifactKind) -> None:
        entry = checkpoint_artifact(
            path=path,
            kind=kind,
            revision_tag=revision_tag,
            run_id=run_id,
            store=store,
        )
        uploaded.append(_entry_to_dict(entry))
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        logger.info(
            "run_id=<%s>, kind=<%s>, artifact_id=<%s>, sha256=<%s> | b2 sync upload ok",
            run_id,
            kind,
            entry.artifact_id,
            entry.sha256[:12],
        )

    # 1. Scenario JSON first — identifies the run even if a later
    #    upload fails.
    if scenario_path:
        scenario = Path(scenario_path)
        if not scenario.is_file():
            raise ValueError(f"scenario_path does not exist: {scenario_path}")
        _upload(scenario, "scene_json")

    # 2. Per-scene wav + mp4, in the order the orchestrator enumerated.
    for entry in clip_artifacts or []:
        if not isinstance(entry, dict):
            raise ValueError(f"clip_artifacts entry not a dict: {entry!r}")
        scene_id = entry.get("scene_id")
        if not scene_id:
            raise ValueError(f"clip_artifacts entry missing scene_id: {entry!r}")
        mp4, wav = _resolve_clip_paths(
            artifact_dir,
            scene_id,
            entry.get("mp4_path"),
            entry.get("wav_path"),
        )
        if wav is not None:
            _upload(wav, "audio_wav")
        if mp4 is not None:
            _upload(mp4, "video_mp4")

    # 3. Master mp4 last — it's the result of every prior upload.
    if master_mp4_path:
        master = Path(master_mp4_path)
        if not master.is_file():
            raise ValueError(f"master_mp4_path does not exist: {master_mp4_path}")
        _upload(master, "master_mp4")

    return {
        "manifest": uploaded,
        "uploaded_count": len(uploaded),
        "kinds": kind_counts,
        "run_id": run_id,
        "revision_tag": revision_tag,
    }


def make_real_b2_sync_tool(
    run_dir: Path,
    *,
    store: B2CheckpointStore,
    default_run_id: str | None = None,
    default_revision_tag: str = "r0001",
) -> Any:
    """Return a LangChain ``@tool`` that wraps :func:`sync_run_artifacts`.

    The returned tool's ``.name`` is ``launch_b2_sync`` so the
    real-worker overlay can swap it in by name. Args mirror the
    placeholder exactly so the demo's scripted ``AIMessage`` works
    against either tool.

    The tool catches every exception and returns the error in the
    envelope's ``args.error`` field — the orchestrator's downstream
    observers expect a stable dict shape, not a raised exception.
    """

    @tool
    def launch_b2_sync(
        artifact_path: str | None = None,
        master_mp4_path: str | None = None,
        clip_artifacts: list[dict[str, Any]] | None = None,
        scenario_path: str | None = None,
        run_id: str | None = None,
        revision_tag: str | None = None,
    ) -> dict[str, Any]:
        """Upload run artifacts to the B2 checkpoint store.

        Args:
            artifact_path: Legacy single-path arg. When set and
                ``master_mp4_path`` is unset, treated as the master
                mp4 (so pre-9h callers still upload one file).
            master_mp4_path: Slice-9g master mp4 (absolute path).
            clip_artifacts: Per-scene ``{"scene_id": str,
                "mp4_path"?: str, "wav_path"?: str}`` entries.
            scenario_path: Absolute path to scenario JSON.
            run_id: Run id to use as the manifest key.
            revision_tag: Preference-ledger revision tag.

        Returns:
            ``{"tool": "launch_b2_sync", "status": "ok"|"error",
            "engine": "b2-checkpoint",
            "args": {"manifest": [...], ...} | {"error": str}}``
            envelope.
        """
        resolved_master = master_mp4_path or artifact_path
        resolved_run_id = run_id or default_run_id or run_dir.name
        resolved_revision = revision_tag or default_revision_tag
        try:
            result = sync_run_artifacts(
                run_dir=run_dir,
                run_id=resolved_run_id,
                revision_tag=resolved_revision,
                store=store,
                master_mp4_path=resolved_master,
                clip_artifacts=clip_artifacts,
                scenario_path=scenario_path,
            )
            return {
                "tool": "launch_b2_sync",
                "status": "ok",
                "engine": "b2-checkpoint",
                "args": result,
            }
        except Exception as exc:  # noqa: BLE001 — surface in envelope, see docstring
            logger.warning(
                "run_id=<%s>, revision=<%s>, error=<%s> | b2 sync failed",
                resolved_run_id,
                resolved_revision,
                exc,
            )
            return {
                "tool": "launch_b2_sync",
                "status": "error",
                "engine": "b2-checkpoint",
                "args": {
                    "error": str(exc),
                    "manifest": [],
                    "uploaded_count": 0,
                    "run_id": resolved_run_id,
                    "revision_tag": resolved_revision,
                },
            }

    return launch_b2_sync


def _resolve_enabled_flag(enabled: bool | None) -> bool:
    """Resolve the ``ENABLE_REAL_B2`` env gate."""
    if enabled is not None:
        return enabled
    raw = os.environ.get("ENABLE_REAL_B2", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _build_store(backend: str | None) -> B2CheckpointStore:
    """Return the store backend selected by ``B2_BACKEND``.

    When ``B2_BACKEND`` is unset, auto-selects ``live`` if both
    ``B2_KEY_ID`` and ``B2_APPLICATION_KEY`` are present in the
    environment, else falls back to ``memory``. This keeps CI
    hermetic (no creds → in-process store) while making the
    production path real-by-default whenever credentials are wired
    (no extra opt-in env var needed).
    """
    explicit = backend if backend is not None else os.environ.get("B2_BACKEND")
    if explicit is None or not explicit.strip():
        has_creds = bool(
            os.environ.get("B2_KEY_ID", "").strip()
            and os.environ.get("B2_APPLICATION_KEY", "").strip()
        )
        backend = "live" if has_creds else "memory"
    else:
        backend = explicit.strip().lower()
    if backend == "live":
        # Lazy import — keeps module import green when b2sdk is not
        # installed. ``LiveB2CheckpointStore`` itself imports b2sdk
        # lazily inside upload/download.
        from strands_agents.b2_checkpoint import LiveB2CheckpointStore

        return LiveB2CheckpointStore()
    return InMemoryB2CheckpointStore()


def build_real_b2_tools(
    *,
    run_dir: Path,
    enabled: bool | None = None,
    store: B2CheckpointStore | None = None,
    default_run_id: str | None = None,
    default_revision_tag: str = "r0001",
) -> dict[str, Any]:
    """Return ``{tool_name: tool}`` overrides for the slice-9h overlay.

    Args:
        run_dir: The orchestrator's run-dir; artifacts under
            ``run_dir/artifacts/`` are what get uploaded.
        enabled: Optional explicit toggle. ``None`` falls through to
            the ``ENABLE_REAL_B2`` env var.
        store: Optional pre-built store (test seam). When ``None``,
            :func:`_build_store` resolves the backend from
            ``B2_BACKEND`` (defaults to in-memory).
        default_run_id: Fallback run id when the tool call doesn't
            carry one. ``None`` falls back to ``run_dir.name``.
        default_revision_tag: Fallback revision tag.

    Returns:
        A dict mapping tool names to ``@tool``-decorated callables.
        Empty when the gate is off — caller falls back to the
        placeholder.
    """
    if not _resolve_enabled_flag(enabled):
        return {}
    try:
        resolved_store = store if store is not None else _build_store(None)
    except Exception as exc:  # noqa: BLE001 — fall back to placeholder
        logger.warning("error=<%s> | %s", exc, _FALLBACK_REASON)
        return {}
    return {
        "launch_b2_sync": make_real_b2_sync_tool(
            run_dir,
            store=resolved_store,
            default_run_id=default_run_id,
            default_revision_tag=default_revision_tag,
        )
    }


def apply_real_b2_overrides(
    base_tools: list[Any],
    overrides: dict[str, Any],
) -> list[Any]:
    """Swap any tool whose ``.name`` matches a key in ``overrides``.

    Preserves list order. Tools not in ``overrides`` pass through
    unchanged. Returns a new list — never mutates the input.
    """
    if not overrides:
        return list(base_tools)
    result: list[Any] = []
    for tool_obj in base_tools:
        name = getattr(tool_obj, "name", None)
        result.append(overrides[name] if name in overrides else tool_obj)
    return result


__all__ = [
    "apply_real_b2_overrides",
    "build_real_b2_tools",
    "make_real_b2_sync_tool",
    "sync_run_artifacts",
]
