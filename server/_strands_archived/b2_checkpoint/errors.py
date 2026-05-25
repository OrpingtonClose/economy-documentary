"""Errors raised by the B2 checkpoint helper.

Each error class is narrow on purpose — downstream consumers (the
orchestrator, the resume loop, the playground experiment) discriminate
on exception type, not on ``.args`` strings. Adding a new invariant
means adding a new error class here.
"""

from __future__ import annotations


class B2CheckpointError(Exception):
    """Base class for every B2 checkpoint failure."""


class StaleRevisionError(B2CheckpointError):
    """An upload carries a revision tag older than the run's latest.

    Revision tags are monotonic per ``run_id``. The orchestrator bumps
    them each time the preference ledger changes; an upload that tries
    to write against an older tag is almost certainly a stale worker
    that missed a ledger update, and its bytes must not be promoted
    into the manifest.

    AGENTS.md invariant 8: *Revision tags are sacred.*
    """

    def __init__(
        self,
        *,
        run_id: str,
        attempted_revision_tag: str,
        latest_revision_tag: str,
    ) -> None:
        self.run_id = run_id
        self.attempted_revision_tag = attempted_revision_tag
        self.latest_revision_tag = latest_revision_tag
        super().__init__(
            f"revision_tag={attempted_revision_tag!r} is older than "
            f"latest={latest_revision_tag!r} for run_id={run_id!r}"
        )


class ManifestMissingError(B2CheckpointError):
    """No manifest exists for the requested ``run_id``.

    Distinct from "manifest exists but is empty" — the two states mean
    different things to the resume loop (unknown run vs. aborted run
    with no artifacts yet).
    """

    def __init__(self, *, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"no manifest found for run_id={run_id!r}")


class ChecksumMismatchError(B2CheckpointError):
    """A downloaded artifact's sha256 does not match its manifest entry.

    Fail-closed: ``resume`` raises this and returns no partial state.
    A bit-flipped artifact cannot be safely promoted to the timeline.
    """

    def __init__(
        self,
        *,
        artifact_id: str,
        expected_sha256: str,
        actual_sha256: str,
    ) -> None:
        self.artifact_id = artifact_id
        self.expected_sha256 = expected_sha256
        self.actual_sha256 = actual_sha256
        super().__init__(
            f"artifact_id={artifact_id!r} sha256 mismatch: "
            f"expected {expected_sha256!r}, got {actual_sha256!r}"
        )


class DuplicateIdempotencyKeyError(B2CheckpointError):
    """Two different artifacts collided on the same idempotency key.

    The helper derives the key from
    ``(run_id, kind, revision_tag, sha256)``; a collision means the
    caller constructed two entries with identical provenance but
    different bytes, which is never valid.
    """

    def __init__(
        self,
        *,
        idempotency_key: str,
        existing_artifact_id: str,
        incoming_sha256: str,
    ) -> None:
        self.idempotency_key = idempotency_key
        self.existing_artifact_id = existing_artifact_id
        self.incoming_sha256 = incoming_sha256
        super().__init__(
            f"idempotency_key={idempotency_key!r} already bound to "
            f"artifact_id={existing_artifact_id!r}; "
            f"incoming sha256={incoming_sha256!r} differs"
        )


__all__ = [
    "B2CheckpointError",
    "ChecksumMismatchError",
    "DuplicateIdempotencyKeyError",
    "ManifestMissingError",
    "StaleRevisionError",
]
