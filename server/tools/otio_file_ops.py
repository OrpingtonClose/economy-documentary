from __future__ import annotations

import fcntl
import glob
import json
import logging
import os
import tempfile
import time
from typing import Any, Callable

import opentimelineio as otio

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical track names — single source of truth
# ---------------------------------------------------------------------------

TRACK_V1: str = "V1_Video"
"""Primary video track."""

TRACK_A1: str = "A1_Narration"
"""Primary narration track."""

TRACK_A2: str = "A2_Music"
"""Music / score track."""


# ---------------------------------------------------------------------------
# Core I/O primitives
# ---------------------------------------------------------------------------

def otio_read(timeline_path: str) -> otio.schema.Timeline:
    """Read an OTIO timeline from disk.

    No locking is needed — atomic writes (see :func:`otio_write`) guarantee
    that the file on disk is never in a torn state.  If the file is being
    written, the reader will see either the previous version or the new
    version, never a half-written file.

    Args:
        timeline_path: Absolute path to the ``.otio`` file.

    Returns:
        The deserialized :class:`otio.schema.Timeline`.

    Raises:
        FileNotFoundError: If *timeline_path* does not exist on disk.
    """
    if not os.path.exists(timeline_path):
        raise FileNotFoundError(f"OTIO timeline not found: {timeline_path}")
    return otio.adapters.read_from_file(timeline_path)


def otio_write(timeline_path: str, timeline: otio.schema.Timeline) -> None:
    """Write an OTIO timeline to disk atomically.

    Serializes to a temporary file in the **same directory** as the target
    (so ``os.rename`` is atomic on the same filesystem), then renames the
    temp file over the target.  This guarantees no torn reads — a
    concurrent reader will see either the old version or the new version,
    never a partially-written file.

    On any failure (serialization error, disk full, permissions), the
    temporary file is cleaned up and the original is left untouched.

    Args:
        timeline_path: Absolute path to the ``.otio`` file.
        timeline: The :class:`otio.schema.Timeline` to persist.
    """
    directory = os.path.dirname(timeline_path) or "."
    os.makedirs(directory, exist_ok=True)

    # Use a predictable prefix so stray temp files are easy to find.
    prefix = f".otio_write_{os.path.basename(timeline_path)}_"
    tmp_fd = None
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=prefix,
            suffix=".otio",
            dir=directory,
        )
        os.close(tmp_fd)
        tmp_fd = None

        otio.adapters.write_to_file(timeline, tmp_path)

        # os.rename is atomic on POSIX when source and dest are on the
        # same filesystem (guaranteed by tempfile.mkstemp(dir=directory)).
        os.rename(tmp_path, timeline_path)
        tmp_path = None  # rename succeeded — nothing to clean up
    except BaseException:
        # Clean up the temp file on ANY failure (including KeyboardInterrupt).
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


# ---------------------------------------------------------------------------
# Read-Modify-Write (RMW) — the ONLY way to mutate the timeline
# ---------------------------------------------------------------------------

def otio_read_modify_write(
    timeline_path: str,
    mutation_fn: Callable[[otio.schema.Timeline], Any],
) -> Any:
    """Read-modify-write cycle with exclusive file locking.

    This is the **only** sanctioned way to mutate the OTIO timeline.
    It acquires ``fcntl.flock(LOCK_EX)`` on a lockfile
    (``{timeline_path}.lock``), reads the current timeline, applies
    *mutation_fn*, writes the result atomically, and releases the lock.

    The lock is held for the entire RMW cycle, preventing concurrent
    writers from interleaving.  Readers don’t need the lock because
    :func:`otio_write` is atomic.

    Args:
        timeline_path: Absolute path to the ``.otio`` file.
        mutation_fn: A callable that receives the current
            :class:`otio.schema.Timeline` and may mutate it in place
            (or return a new one).  Its return value is forwarded to
            the caller.

    Returns:
        Whatever *mutation_fn* returns.

    Raises:
        FileNotFoundError: If the timeline file does not exist.
    """
    lock_path = f"{timeline_path}.lock"
    lock_fd = None
    try:
        # Ensure the lockfile exists so we can open it.  The directory
        # must already exist (otio_write creates it).
        lock_dir = os.path.dirname(lock_path) or "."
        os.makedirs(lock_dir, exist_ok=True)
        lock_fd = open(lock_path, "w")  # noqa: SIM115 — we close in finally

        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        timeline = otio_read(timeline_path)
        result = mutation_fn(timeline)
        otio_write(timeline_path, timeline)
        return result
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                lock_fd.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Context-manager lock for multi-operation hold
# ---------------------------------------------------------------------------

class OTIOFileLock:
    """Context manager that holds an exclusive ``fcntl.flock`` on a lockfile.

    Use this when you need to hold the lock across multiple operations
    (e.g. read, inspect, conditionally write) without going through
    :func:`otio_read_modify_write`.

    Usage::

        with OTIOFileLock(timeline_path) as lock:
            timeline = otio_read(timeline_path)
            # ... inspect / mutate ...
            otio_write(timeline_path, timeline)

    The lockfile is ``{timeline_path}.lock`` — the same one used by
    :func:`otio_read_modify_write`, so the two are mutually exclusive.

    Args:
        timeline_path: Absolute path to the ``.otio`` file.
    """

    def __init__(self, timeline_path: str) -> None:
        self._lock_path = f"{timeline_path}.lock"
        self._lock_fd: object | None = None

    def __enter__(self) -> "OTIOFileLock":
        lock_dir = os.path.dirname(self._lock_path) or "."
        os.makedirs(lock_dir, exist_ok=True)
        self._lock_fd = open(self._lock_path, "w")  # noqa: SIM115
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: object) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._lock_fd.close()
            except OSError:
                pass
            self._lock_fd = None


# ---------------------------------------------------------------------------
# Path resolution — discover the timeline from the pipeline manifest
# ---------------------------------------------------------------------------

def resolve_timeline_path(pipeline_dir: str | None = None) -> str:
    """Discover the OTIO timeline path from the pipeline manifest.

    Reads ``pipeline_manifest.json`` in the pipeline directory to find
    the ``timeline_path`` entry.  This is the canonical way for
    stateless agents to locate the timeline — no env-var passing, no
    in-memory blackboard.

    Resolution order:

    1. *pipeline_dir* argument (if provided).
    2. ``PIPELINE_DIR`` environment variable.
    3. Raises :class:`FileNotFoundError` if neither is available.

    The manifest file must contain a ``"timeline_path"`` key whose value
    is an existing file.  If the key is missing or the file doesn’t exist,
    :class:`FileNotFoundError` is raised.

    Args:
        pipeline_dir: Optional override for the pipeline directory.
            Falls back to ``os.environ["PIPELINE_DIR"]``.

    Returns:
        Absolute path to the OTIO timeline file.

    Raises:
        FileNotFoundError: If the manifest is missing, the
            ``timeline_path`` key is absent, or the timeline file
            doesn’t exist.
    """
    if pipeline_dir is None:
        pipeline_dir = os.environ.get("PIPELINE_DIR")

    if not pipeline_dir:
        raise FileNotFoundError(
            "Cannot resolve timeline path: pipeline_dir not provided "
            "and PIPELINE_DIR env var is not set"
        )

    manifest_path = os.path.join(pipeline_dir, "pipeline_manifest.json")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(
            f"Pipeline manifest not found: {manifest_path}"
        )

    try:
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
    except json.JSONDecodeError as exc:
        raise FileNotFoundError(
            f"Pipeline manifest is not valid JSON: {manifest_path} ({exc})"
        ) from exc

    timeline_path = manifest.get("timeline_path")
    if not timeline_path:
        raise FileNotFoundError(
            f"Pipeline manifest missing 'timeline_path' key: {manifest_path}"
        )

    if not os.path.exists(timeline_path):
        raise FileNotFoundError(
            f"Timeline file from manifest does not exist: {timeline_path}"
        )

    return timeline_path


# ---------------------------------------------------------------------------
# Checkpoint — versioned stage snapshots
# ---------------------------------------------------------------------------

def _checkpoint_paths(
    checkpoint_dir: str,
    run_id: str,
    label: str,
    timestamp: int,
) -> tuple[str, str]:
    """Compute snapshot and sidecar paths for a checkpoint.

    Returns:
        ``(snapshot_path, meta_path)`` inside *checkpoint_dir*.
    """
    suffix = f"{run_id}_{label}_{timestamp}" if run_id else f"{label}_{timestamp}"
    snapshot_path = os.path.join(checkpoint_dir, f"{suffix}.otio")
    meta_path = os.path.join(checkpoint_dir, f"{suffix}.meta.json")
    return snapshot_path, meta_path


# ---------------------------------------------------------------------------
# Checkpoint listing — discover available stage snapshots
# ---------------------------------------------------------------------------

def list_checkpoints(
    timeline_dir: str,
    run_id: str = "",
) -> list[dict[str, Any]]:
    """List available OTIO checkpoints under *timeline_dir*.

    Scans ``{timeline_dir}/.checkpoints/`` for snapshot files written
    by :func:`otio_checkpoint`.  Each checkpoint entry describes the
    stage label, the epoch timestamp, and optional sidecar metadata.

    Filter & sort behaviour
    -----------------------
    **Filter** — When *run_id* is non-empty, only checkpoints whose
    sidecar ``run_id`` field matches exactly are included.  The default
    empty string disables filtering and includes every checkpoint.

    **Sort order** — The returned list is ordered **descending by
    timestamp** (newest checkpoint first).  This guarantees that
    ``result[0]`` is always the latest snapshot, which resume logic
    relies on.

    Each dict contains at minimum:

    - ``checkpoint_path`` (*str*): Absolute path to the ``.otio`` snapshot.
    - ``label`` (*str*): Stage label, e.g. ``"scenario"`` or ``"audio"``.
    - ``timestamp`` (*float* | *int*): Epoch seconds when the checkpoint
      was created.
    - ``meta_path`` (*str* | *None*): Path to the ``.meta.json`` sidecar,
      if present.
    - ``clip_counts`` (*dict[str, int]* | *None*): Track-to-clip-count
      mapping from sidecar metadata.
    - ``completed`` (*bool* | *None*): Whether the stage gate has
      marked this checkpoint as finished.

    Args:
        timeline_dir: Absolute path to the directory that contains the
            live timeline.  The checkpoint store is expected at
            ``{timeline_dir}/.checkpoints``.
        run_id: Optional run identifier used to filter checkpoints by
            exact match against the sidecar ``run_id`` field.

    Returns:
        A list of checkpoint metadata dicts.  Returns ``[]`` when the
        ``.checkpoints`` directory is missing or contains no matching
        entries.

    Raises:
        FileNotFoundError: If *timeline_dir* itself does not exist.
    """
    if not os.path.exists(timeline_dir):
        raise FileNotFoundError(f"Timeline directory not found: {timeline_dir}")

    checkpoint_dir = os.path.join(timeline_dir, ".checkpoints")
    if not os.path.exists(checkpoint_dir):
        return []

    pattern = os.path.join(checkpoint_dir, "*.meta.json")
    meta_paths = glob.glob(pattern)

    checkpoints: list[dict[str, Any]] = []
    for meta_path in meta_paths:
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                sidecar = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        if run_id and sidecar.get("run_id") != run_id:
            continue

        otio_path = meta_path.replace(".meta.json", ".otio")
        if not os.path.exists(otio_path):
            continue

        entry: dict[str, Any] = {
            "checkpoint_path": otio_path,
            "label": sidecar.get("label", ""),
            "timestamp": sidecar.get("timestamp", 0),
            "meta_path": meta_path,
            "clip_counts": sidecar.get("clip_counts"),
            "completed": sidecar.get("completed"),
        }
        checkpoints.append(entry)

    checkpoints.sort(key=lambda x: x["timestamp"], reverse=True)
    return checkpoints