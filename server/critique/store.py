"""Persistent store for :class:`critique.record.ArtifactCritiqueRecord`.

Disk-first persistence under::

    <root>/critiques/<artifact_type>/<artifact_id>.json

with an optional B2 mirror at the same relative key (``critiques/<type>/<id>.json``)
under the run's B2 prefix — matching the existing checkpoint layout in
:mod:`tools.b2_checkpoint`.

The store is **append-biased**: the public helpers read the current record
(or create a blank one), append a new :class:`Critique` / :class:`QaVerdict`
/ :class:`EscalationRef`, then write the merged record back.  This keeps
concurrent writers from clobbering each other as long as they do not race
on the *same* ``(artifact_type, artifact_id)``.  A per-file lock is used
inside a single process; cross-process writers should coordinate at a
higher level (today the pipeline is single-process per run).

The B2 upload is best-effort — the store never raises if the B2 mirror
fails; the disk copy is authoritative.  This keeps the critique layer
usable in environments without B2 credentials (CI, unit tests).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from critique.record import (
    ARTIFACT_TYPES,
    ArtifactCritiqueRecord,
    ArtifactType,
    Critique,
    EscalationRef,
    QaVerdict,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_CRITIQUE_SUBDIR = "critiques"


def _default_root() -> Path:
    """Return the default on-disk root for critique records.

    Mirrors :mod:`tools.b2_checkpoint`'s local-cache layout: runs are
    scoped by the ``B2_RUN_ID`` env var (falling back to a generic
    ``default`` run if unset) under ``/tmp/documentary-pipeline``.
    Tests always inject an explicit ``root=`` so this default never
    surprises them.
    """

    base = os.environ.get(
        "CRITIQUE_STORE_ROOT",
        "/tmp/documentary-pipeline",
    )
    run_id = os.environ.get("B2_RUN_ID", "default")
    return Path(base) / run_id


def _safe_component(value: str) -> str:
    """Sanitise a path component so it does not escape the store root.

    ``artifact_id`` is usually something like ``s003_p002`` but callers
    might pass free-form strings; this strips path separators and null
    bytes defensively.  Empty input raises ``ValueError`` rather than
    silently writing to the parent directory.
    """

    cleaned = value.replace("\x00", "").strip()
    cleaned = cleaned.replace("/", "_").replace("\\", "_").replace("..", "_")
    if not cleaned:
        raise ValueError(f"refusing to use empty/unsafe path component: {value!r}")
    return cleaned


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class ArtifactCritiqueStore:
    """Disk-backed store of :class:`ArtifactCritiqueRecord` instances.

    Parameters
    ----------
    root:
        Root directory for the on-disk copy.  ``<root>/critiques/<type>/<id>.json``
        is where records live.  If ``None`` the default derived from
        ``B2_RUN_ID`` is used.
    b2_enabled:
        Whether to mirror writes to Backblaze B2 via
        :mod:`tools.b2_checkpoint`.  Failures are logged but not raised.
        Tests pass ``False`` so they never touch the network.
    """

    def __init__(
        self,
        root: Optional[Path | str] = None,
        *,
        b2_enabled: bool = True,
    ) -> None:
        if root is None:
            self._root = _default_root()
        else:
            self._root = Path(root)
        self._b2_enabled = b2_enabled
        self._lock = threading.RLock()
        (self._root / _CRITIQUE_SUBDIR).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, artifact_type: ArtifactType, artifact_id: str) -> Path:
        if artifact_type not in ARTIFACT_TYPES:
            raise ValueError(
                f"unknown artifact_type {artifact_type!r}; "
                f"valid: {ARTIFACT_TYPES}"
            )
        safe_type = _safe_component(artifact_type)
        safe_id = _safe_component(artifact_id)
        directory = self._root / _CRITIQUE_SUBDIR / safe_type
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{safe_id}.json"

    def _b2_key(self, artifact_type: ArtifactType, artifact_id: str) -> str:
        return (
            f"{_CRITIQUE_SUBDIR}/"
            f"{_safe_component(artifact_type)}/"
            f"{_safe_component(artifact_id)}.json"
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(
        self,
        artifact_type: ArtifactType,
        artifact_id: str,
    ) -> Optional[ArtifactCritiqueRecord]:
        """Return the record for ``(artifact_type, artifact_id)``, or None.

        Corrupt JSON is logged and treated as absent so a bad on-disk
        copy never takes down the escalation path.
        """

        path = self._path(artifact_type, artifact_id)
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return ArtifactCritiqueRecord.from_dict(data)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "critique store: failed to read %s/%s: %s",
                artifact_type, artifact_id, exc,
            )
            return None

    def list_ids(
        self,
        artifact_type: Optional[ArtifactType] = None,
    ) -> list[tuple[str, str]]:
        """Return ``(artifact_type, artifact_id)`` pairs on disk.

        When ``artifact_type`` is given, only that subdirectory is
        scanned; otherwise all subdirectories are walked.
        """

        pairs: list[tuple[str, str]] = []
        base = self._root / _CRITIQUE_SUBDIR
        if not base.exists():
            return pairs

        types: Iterable[str]
        if artifact_type is None:
            types = [p.name for p in base.iterdir() if p.is_dir()]
        else:
            if artifact_type not in ARTIFACT_TYPES:
                raise ValueError(
                    f"unknown artifact_type {artifact_type!r}; "
                    f"valid: {ARTIFACT_TYPES}"
                )
            types = [artifact_type]

        for t in types:
            d = base / t
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.suffix == ".json" and p.is_file():
                    pairs.append((t, p.stem))
        return pairs

    def read_all(
        self,
        artifact_type: Optional[ArtifactType] = None,
    ) -> list[ArtifactCritiqueRecord]:
        """Read every record (optionally filtered by type) into memory."""

        out: list[ArtifactCritiqueRecord] = []
        for t, aid in self.list_ids(artifact_type):
            rec = self.read(t, aid)  # type: ignore[arg-type]
            if rec is not None:
                out.append(rec)
        return out

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write(self, record: ArtifactCritiqueRecord) -> None:
        """Replace the on-disk record with ``record`` (atomic rename).

        Callers who want to append a single entry should prefer the
        ``append_*`` helpers, which handle the read-modify-write cycle.
        """

        record.updated_at = time.time()
        path = self._path(record.artifact_type, record.artifact_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        data = record.to_dict()
        with self._lock:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        self._mirror_to_b2(record, data)

    def _get_or_blank(
        self,
        artifact_type: ArtifactType,
        artifact_id: str,
    ) -> ArtifactCritiqueRecord:
        existing = self.read(artifact_type, artifact_id)
        if existing is not None:
            return existing
        return ArtifactCritiqueRecord(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
        )

    def append_critique(
        self,
        artifact_type: ArtifactType,
        artifact_id: str,
        critique: Critique,
        *,
        produced_by: str = "",
        iteration: Optional[int] = None,
    ) -> ArtifactCritiqueRecord:
        """Append a :class:`Critique` to the record, creating it if absent.

        ``produced_by`` / ``iteration`` are merged in only when set — the
        first writer wins on ``produced_by`` to record the *creator* of
        the artifact (not whoever critiqued it last).
        """

        with self._lock:
            record = self._get_or_blank(artifact_type, artifact_id)
            if produced_by and not record.produced_by:
                record.produced_by = produced_by
            if iteration is not None:
                record.iteration = max(record.iteration, iteration)
            record.critiques.append(critique)
            self.write(record)
            return record

    def append_qa(
        self,
        artifact_type: ArtifactType,
        artifact_id: str,
        verdict: QaVerdict,
        *,
        produced_by: str = "",
        iteration: Optional[int] = None,
    ) -> ArtifactCritiqueRecord:
        """Append a :class:`QaVerdict` to the record, creating it if absent."""

        with self._lock:
            record = self._get_or_blank(artifact_type, artifact_id)
            if produced_by and not record.produced_by:
                record.produced_by = produced_by
            if iteration is not None:
                record.iteration = max(record.iteration, iteration)
            record.qa_results.append(verdict)
            self.write(record)
            return record

    def append_escalation(
        self,
        artifact_type: ArtifactType,
        artifact_id: str,
        ref: EscalationRef,
    ) -> ArtifactCritiqueRecord:
        """Append an :class:`EscalationRef` to the record."""

        with self._lock:
            record = self._get_or_blank(artifact_type, artifact_id)
            record.escalations.append(ref)
            self.write(record)
            return record

    # ------------------------------------------------------------------
    # B2 mirror (best-effort)
    # ------------------------------------------------------------------

    def _mirror_to_b2(
        self,
        record: ArtifactCritiqueRecord,
        data: dict[str, Any],
    ) -> None:
        if not self._b2_enabled:
            return
        key = self._b2_key(record.artifact_type, record.artifact_id)
        try:
            # Imported lazily so test environments without b2sdk still
            # exercise the disk path without pulling the dependency.
            from tools import b2_checkpoint as _b2  # type: ignore

            uploader = getattr(_b2, "upload_json", None)
            if uploader is None:
                return
            uploader(data, key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("critique store: B2 mirror failed for %s: %s", key, exc)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_store: Optional[ArtifactCritiqueStore] = None
_singleton_lock = threading.Lock()


def get_critique_store() -> ArtifactCritiqueStore:
    """Return the process-wide :class:`ArtifactCritiqueStore`.

    The singleton is created lazily the first time it is requested, so
    importing :mod:`critique` does not create on-disk state.  Tests that
    want isolation should construct an :class:`ArtifactCritiqueStore`
    directly rather than mutating this singleton.
    """

    global _store
    with _singleton_lock:
        if _store is None:
            _store = ArtifactCritiqueStore()
        return _store


def set_critique_store(store: Optional[ArtifactCritiqueStore]) -> None:
    """Replace the module-level singleton (primarily for tests).

    ``None`` clears it so the next call to :func:`get_critique_store`
    re-creates a default instance.
    """

    global _store
    with _singleton_lock:
        _store = store
