"""B2 checkpoint helper — per-run artifact ledger + resume.

Wave 2 slice 6. Every artifact the pipeline produces — scene JSON,
rendered WAV, rendered MP4, whisperx alignment, OTIO XML, master MP4
— passes through ``checkpoint_artifact`` before the next stage
consumes it. The manifest for a run lives alongside its artifacts in
B2, so a crashed run can be resumed by reading the manifest and
replaying the ledger.

Three public entry points:

* :func:`checkpoint_artifact` — upload + manifest entry (idempotent,
  monotonic on revision tag, content-addressed).
* :func:`load_manifest` — read the manifest for a run.
* :func:`resume` — verify every artifact and return a
  :class:`ResumeState` the orchestrator can act on.

The :class:`InMemoryB2CheckpointStore` implementation is what the
strands-evals playground Experiment drives — no real B2 traffic in CI
or in UI runs. The :class:`LiveB2CheckpointStore` stub is present for
parity; it will be wired up when the orchestrator integrates this
helper (a later slice).

AGENTS.md invariant 6 — *every artifact to B2 immediately* — is what
this module exists to honour. Invariant 8 — *revision tags are
sacred* — is enforced here by :class:`StaleRevisionError`.
"""

from __future__ import annotations

from pathlib import Path

from strands_agents.b2_checkpoint.errors import (
    B2CheckpointError,
    ChecksumMismatchError,
    DuplicateIdempotencyKeyError,
    ManifestMissingError,
    StaleRevisionError,
)
from strands_agents.b2_checkpoint.manifest import (
    ARTIFACT_KINDS,
    ArtifactKind,
    Manifest,
    ManifestEntry,
)
from strands_agents.b2_checkpoint.resume import ResumeState, resume
from strands_agents.b2_checkpoint.store import (
    B2CheckpointStore,
    InMemoryB2CheckpointStore,
    LiveB2CheckpointStore,
)


def checkpoint_artifact(
    *,
    path: Path,
    kind: ArtifactKind,
    revision_tag: str,
    run_id: str,
    store: B2CheckpointStore,
) -> ManifestEntry:
    """Upload ``path`` to ``store`` and return its manifest entry.

    The function reads the file into memory and delegates to
    :meth:`B2CheckpointStore.upload`, which is responsible for the
    idempotency and stale-revision invariants. Callers never construct
    :class:`ManifestEntry` directly — every entry in the ledger comes
    from this function or the store's own ``upload``.

    Raises:
        StaleRevisionError: ``revision_tag`` is older than the run's
            current latest.
        DuplicateIdempotencyKeyError: two different artifacts produced
            the same idempotency key with different bytes (not
            reachable on the normal code path).
    """
    payload = path.read_bytes()
    return store.upload(
        payload=payload,
        kind=kind,
        revision_tag=revision_tag,
        run_id=run_id,
    )


def load_manifest(*, run_id: str, store: B2CheckpointStore) -> Manifest:
    """Return the manifest for ``run_id``.

    Raises:
        ManifestMissingError: the store has no manifest for ``run_id``.
    """
    return store.list_for_run(run_id)


__all__ = ['ARTIFACT_KINDS', 'ArtifactKind', 'B2CheckpointError', 'B2CheckpointStore', 'ChecksumMismatchError', 'DuplicateIdempotencyKeyError', 'InMemoryB2CheckpointStore', 'LiveB2CheckpointStore', 'Manifest', 'ManifestEntry', 'ManifestMissingError', 'Path', 'ResumeState', 'StaleRevisionError', 'annotations', 'checkpoint_artifact', 'load_manifest', 'resume']
