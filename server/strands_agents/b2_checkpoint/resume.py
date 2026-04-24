"""Resume — replay a B2 manifest into a structured pipeline state.

``resume(run_id, store)`` is what the orchestrator calls to pick up an
interrupted run. It reads the manifest, verifies every artifact's
sha256 by downloading into a working directory, and returns a
:class:`ResumeState` describing what's present, what's missing, and
what's stale.

The resume contract is **fail-closed**: any checksum mismatch aborts
with :class:`ChecksumMismatchError` — no partial state escapes. A
bit-flipped artifact cannot be safely promoted back into the pipeline.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from strands_agents.b2_checkpoint.manifest import (
    ARTIFACT_KINDS,
    ArtifactKind,
    ManifestEntry,
)


class _StoreForResume(Protocol):
    """The slice of :class:`B2CheckpointStore` that resume needs."""

    def list_for_run(self, run_id: str):  # -> Manifest
        ...

    def download(self, *, artifact_id: str, dest: Path) -> Path:
        ...


@dataclass(frozen=True)
class ResumeState:
    """What the orchestrator gets back from a successful resume.

    Attributes:
        run_id: The run being resumed.
        latest_revision_tag: The highest revision tag in the manifest,
            or ``None`` if the manifest is empty.
        artifacts_by_kind: Mapping from :data:`ARTIFACT_KINDS` members
            to the entries with that kind, in upload order. Every kind
            is represented, even if empty, so downstream consumers
            never ``KeyError``.
        missing_kinds: Kinds that have zero entries — a convenience
            for the orchestrator's "what's left to do" decision.
        stale_revision_entries: Entries whose revision tag is strictly
            older than :attr:`latest_revision_tag`. Not an error — the
            ledger IS append-only — but the orchestrator should not
            promote these back into the working timeline.
    """

    run_id: str
    latest_revision_tag: str | None
    artifacts_by_kind: dict[ArtifactKind, tuple[ManifestEntry, ...]]
    missing_kinds: tuple[ArtifactKind, ...]
    stale_revision_entries: tuple[ManifestEntry, ...] = field(
        default_factory=tuple
    )


def resume(
    *,
    run_id: str,
    store: _StoreForResume,
    working_dir: Path | None = None,
) -> ResumeState:
    """Reconstruct :class:`ResumeState` for ``run_id``.

    Downloads every manifest entry into ``working_dir`` (a fresh temp
    directory by default), verifying sha256 on each. The first
    checksum failure raises :class:`ChecksumMismatchError` and no
    partial state is returned — fail-closed by design.

    The orchestrator keeps the returned paths implicit; what it uses
    downstream is the structural view in :class:`ResumeState`, not the
    bytes. Callers that do need the bytes should walk the manifest
    again with their own :meth:`download` calls.

    Raises:
        ManifestMissingError: ``run_id`` has no manifest in the store.
        ChecksumMismatchError: any entry's bytes disagree with its
            recorded ``sha256``.
    """
    manifest = store.list_for_run(run_id)
    latest = manifest.latest_revision_tag

    by_kind: dict[ArtifactKind, tuple[ManifestEntry, ...]] = {
        kind: () for kind in ARTIFACT_KINDS
    }
    for kind in ARTIFACT_KINDS:
        by_kind[kind] = tuple(
            e for e in manifest.entries if e.kind == kind
        )

    missing = tuple(
        kind for kind in ARTIFACT_KINDS if not by_kind[kind]
    )

    stale: list[ManifestEntry] = []
    if latest is not None:
        for entry in manifest.entries:
            if entry.revision_tag < latest:
                stale.append(entry)

    # Verify every artifact by downloading through the store — this is
    # where a bit-flip is caught. The store's ``download`` raises
    # ``ChecksumMismatchError`` on failure; we let it propagate so the
    # caller learns about it atomically.
    if working_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="b2-resume-")
        _working = Path(tmp.name)
    else:
        tmp = None
        _working = working_dir
        _working.mkdir(parents=True, exist_ok=True)

    try:
        for entry in manifest.entries:
            dest = _working / entry.kind / entry.artifact_id
            store.download(artifact_id=entry.artifact_id, dest=dest)
    finally:
        if tmp is not None:
            tmp.cleanup()

    return ResumeState(
        run_id=run_id,
        latest_revision_tag=latest,
        artifacts_by_kind=by_kind,
        missing_kinds=missing,
        stale_revision_entries=tuple(stale),
    )


__all__ = ["ResumeState", "resume"]
