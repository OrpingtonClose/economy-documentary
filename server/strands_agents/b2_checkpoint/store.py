"""B2 checkpoint store — protocol + in-memory + live implementations.

Three surface methods, in order of use in the pipeline:

* :meth:`B2CheckpointStore.upload` — content-addressed, idempotent,
  monotonic on revision tag.
* :meth:`B2CheckpointStore.list_for_run` — return the ledger in upload
  order.
* :meth:`B2CheckpointStore.download` — fetch artifact bytes to disk
  and verify sha256.

The in-memory implementation is what the strands-evals playground
Experiment drives — no real B2 traffic in CI or in UI runs. The live
implementation exists for production use (wired up in a later slice).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from strands_agents.b2_checkpoint.errors import (
    ChecksumMismatchError,
    DuplicateIdempotencyKeyError,
    ManifestMissingError,
    StaleRevisionError,
)
from strands_agents.b2_checkpoint.manifest import (
    ArtifactKind,
    Manifest,
    ManifestEntry,
)

logger = logging.getLogger(__name__)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _idempotency_key(
    *, run_id: str, kind: str, revision_tag: str, sha256: str
) -> str:
    return f"{run_id}:{kind}:{revision_tag}:{sha256}"


def _b2_key(
    *,
    run_id: str,
    kind: str,
    revision_tag: str,
    artifact_id: str,
) -> str:
    # Flat namespace under the run — human-readable, collision-free.
    return f"runs/{run_id}/{kind}/{revision_tag}/{artifact_id}"


@runtime_checkable
class B2CheckpointStore(Protocol):
    """Contract every checkpoint backend honours.

    The production :class:`LiveB2CheckpointStore` wraps ``b2sdk.v2``.
    The :class:`InMemoryB2CheckpointStore` is what tests and the
    playground Experiment drive.
    """

    def upload(
        self,
        *,
        payload: bytes,
        kind: ArtifactKind,
        revision_tag: str,
        run_id: str,
    ) -> ManifestEntry:
        """Upload ``payload`` and return its manifest entry."""
        ...

    def download(self, *, artifact_id: str, dest: Path) -> Path:
        """Download ``artifact_id`` to ``dest``, verify sha256."""
        ...

    def list_for_run(self, run_id: str) -> Manifest:
        """Return the full manifest for ``run_id`` in upload order."""
        ...

    def exists(self, artifact_id: str) -> bool:
        """Return whether ``artifact_id`` is in the store."""
        ...


class InMemoryB2CheckpointStore:
    """In-memory checkpoint store for tests + strands-evals.

    Keeps artifact bytes and manifests in dicts; no B2 traffic. The
    strands-evals playground Experiment drives this implementation
    exclusively — production use goes through :class:`LiveB2CheckpointStore`.
    """

    def __init__(self) -> None:
        # artifact_id -> bytes
        self._blobs: dict[str, bytes] = {}
        # artifact_id -> manifest entry
        self._entries: dict[str, ManifestEntry] = {}
        # idempotency_key -> artifact_id
        self._by_idem: dict[str, str] = {}
        # run_id -> list[artifact_id] (upload order)
        self._run_order: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def upload(
        self,
        *,
        payload: bytes,
        kind: ArtifactKind,
        revision_tag: str,
        run_id: str,
    ) -> ManifestEntry:
        sha256 = _sha256_bytes(payload)
        idem = _idempotency_key(
            run_id=run_id, kind=kind, revision_tag=revision_tag, sha256=sha256
        )

        with self._lock:
            existing_artifact_id = self._by_idem.get(idem)
            if existing_artifact_id is not None:
                # Idempotent replay — same bytes, same revision, same kind.
                return self._entries[existing_artifact_id]

            # Monotonic revision guard. "Older" is compared lexicographically
            # on the caller-supplied string; revision tags are expected to
            # be string-orderable (e.g. ``r0001``, ``r0002``) per the
            # existing scenario-refiner convention.
            run_entries = [
                self._entries[aid]
                for aid in self._run_order.get(run_id, [])
            ]
            if run_entries:
                latest_tag = max(e.revision_tag for e in run_entries)
                if revision_tag < latest_tag:
                    raise StaleRevisionError(
                        run_id=run_id,
                        attempted_revision_tag=revision_tag,
                        latest_revision_tag=latest_tag,
                    )

            # Idempotency-collision guard — same key with different bytes
            # is always a caller bug. Cannot be reached via the normal
            # code path (the key embeds the sha256), but guards against
            # a bypass that pre-computes the key elsewhere.
            for existing_entry in run_entries:
                if (
                    existing_entry.idempotency_key == idem
                    and existing_entry.sha256 != sha256
                ):
                    raise DuplicateIdempotencyKeyError(
                        idempotency_key=idem,
                        existing_artifact_id=existing_entry.artifact_id,
                        incoming_sha256=sha256,
                    )

            artifact_id = f"art-{uuid.uuid4().hex[:12]}"
            entry = ManifestEntry(
                artifact_id=artifact_id,
                run_id=run_id,
                revision_tag=revision_tag,
                kind=kind,
                b2_key=_b2_key(
                    run_id=run_id,
                    kind=kind,
                    revision_tag=revision_tag,
                    artifact_id=artifact_id,
                ),
                sha256=sha256,
                size_bytes=len(payload),
                uploaded_at_iso=_now_iso(),
                idempotency_key=idem,
            )
            self._blobs[artifact_id] = payload
            self._entries[artifact_id] = entry
            self._by_idem[idem] = artifact_id
            self._run_order.setdefault(run_id, []).append(artifact_id)
            logger.debug(
                "run_id=<%s>, artifact_id=<%s>, kind=<%s>, revision_tag=<%s> "
                "| b2 checkpoint upload recorded",
                run_id,
                artifact_id,
                kind,
                revision_tag,
            )
            return entry

    def download(self, *, artifact_id: str, dest: Path) -> Path:
        with self._lock:
            entry = self._entries.get(artifact_id)
            payload = self._blobs.get(artifact_id)
        if entry is None or payload is None:
            raise ManifestMissingError(run_id=f"(unknown-artifact:{artifact_id})")

        actual = _sha256_bytes(payload)
        if actual != entry.sha256:
            raise ChecksumMismatchError(
                artifact_id=artifact_id,
                expected_sha256=entry.sha256,
                actual_sha256=actual,
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return dest

    def list_for_run(self, run_id: str) -> Manifest:
        with self._lock:
            order = self._run_order.get(run_id)
            if order is None:
                raise ManifestMissingError(run_id=run_id)
            entries = tuple(self._entries[aid] for aid in order)
        return Manifest(run_id=run_id, entries=entries)

    def exists(self, artifact_id: str) -> bool:
        with self._lock:
            return artifact_id in self._entries

    # ── Test / experiment hooks (explicitly *not* in the Protocol) ──

    def _corrupt_for_testing(self, *, artifact_id: str, new_bytes: bytes) -> None:
        """Replace an artifact's bytes without touching its manifest.

        Used by the strands-evals ``resume_checksum_mismatch_fails_closed``
        Case to simulate bit-rot. Not part of the Protocol and not
        callable from production code paths.
        """
        with self._lock:
            if artifact_id not in self._entries:
                raise KeyError(artifact_id)
            self._blobs[artifact_id] = new_bytes


class LiveB2CheckpointStore:
    """Production B2-backed implementation (wired up in a later slice).

    Reads ``B2_KEY_ID`` and ``B2_APPLICATION_KEY`` from the environment
    on construction. Uploads and downloads go through ``b2sdk.v2``.

    The class lives here for shape/parity with :class:`InMemoryB2CheckpointStore`
    but is NOT exercised by any strands-evals Case — production
    traffic stays out of CI and UI runs by construction. A later
    slice will wire this into the orchestrator and add a sandbox card
    for driving a real upload from the UI.
    """

    def __init__(
        self,
        *,
        bucket_name: str | None = None,
        key_id_env: str = "B2_KEY_ID",
        application_key_env: str = "B2_APPLICATION_KEY",
        bucket_env: str = "B2_BUCKET_NAME",
    ) -> None:
        key_id = os.environ.get(key_id_env)
        application_key = os.environ.get(application_key_env)
        if not key_id or not application_key:
            raise RuntimeError(
                f"B2 credentials missing: set {key_id_env} and "
                f"{application_key_env} in the environment before "
                f"constructing LiveB2CheckpointStore"
            )
        resolved_bucket = (
            bucket_name
            or os.environ.get(bucket_env)
            or "documentary-checkpoints"
        )
        self._bucket_name = resolved_bucket
        self._key_id = key_id
        self._application_key = application_key
        # b2sdk client + bucket handle are constructed lazily inside
        # ``_ensure_client`` so import of this module does not depend
        # on b2sdk being installed. Keeps the strands-evals Experiment
        # (which only imports the Protocol) free of runtime deps.
        self._client: object | None = None
        self._bucket: object | None = None
        self._lock = threading.Lock()
        self._entries: dict[str, ManifestEntry] = {}
        self._by_idem: dict[str, str] = {}
        self._run_order: dict[str, list[str]] = {}

    def _ensure_client(self) -> tuple[object, object]:
        """Lazily build the b2sdk client + bucket handle.

        Held under ``self._lock`` so concurrent first-touch upload
        calls don't authorise twice.
        """
        with self._lock:
            if self._bucket is not None:
                return self._client, self._bucket  # type: ignore[return-value]
            from b2sdk.v2 import B2Api, InMemoryAccountInfo

            info = InMemoryAccountInfo()
            api = B2Api(info)
            api.authorize_account("production", self._key_id, self._application_key)
            bucket = api.get_bucket_by_name(self._bucket_name)
            self._client = api
            self._bucket = bucket
            return api, bucket

    def upload(
        self,
        *,
        payload: bytes,
        kind: ArtifactKind,
        revision_tag: str,
        run_id: str,
    ) -> ManifestEntry:
        sha256 = _sha256_bytes(payload)
        idem = _idempotency_key(
            run_id=run_id, kind=kind, revision_tag=revision_tag, sha256=sha256
        )
        with self._lock:
            existing_id = self._by_idem.get(idem)
            if existing_id is not None:
                return self._entries[existing_id]

        artifact_id = f"art-{uuid.uuid4().hex[:12]}"
        b2_key = _b2_key(
            run_id=run_id,
            kind=kind,
            revision_tag=revision_tag,
            artifact_id=artifact_id,
        )
        _, bucket = self._ensure_client()
        # ``upload_bytes`` is the b2sdk.v2 surface for uploading raw
        # payloads. Returns a ``FileVersion``.
        bucket.upload_bytes(  # type: ignore[union-attr]
            data_bytes=payload,
            file_name=b2_key,
        )
        entry = ManifestEntry(
            artifact_id=artifact_id,
            run_id=run_id,
            revision_tag=revision_tag,
            kind=kind,
            b2_key=b2_key,
            sha256=sha256,
            size_bytes=len(payload),
            uploaded_at_iso=_now_iso(),
            idempotency_key=idem,
        )
        with self._lock:
            self._entries[artifact_id] = entry
            self._by_idem[idem] = artifact_id
            self._run_order.setdefault(run_id, []).append(artifact_id)
        logger.info(
            "run_id=<%s>, artifact_id=<%s>, kind=<%s>, b2_key=<%s>, size=<%d> "
            "| live b2 upload ok",
            run_id,
            artifact_id,
            kind,
            b2_key,
            len(payload),
        )
        return entry

    def download(self, *, artifact_id: str, dest: Path) -> Path:
        with self._lock:
            entry = self._entries.get(artifact_id)
        if entry is None:
            raise ManifestMissingError(run_id=f"(unknown-artifact:{artifact_id})")
        _, bucket = self._ensure_client()
        dest.parent.mkdir(parents=True, exist_ok=True)
        downloaded = bucket.download_file_by_name(entry.b2_key)  # type: ignore[union-attr]
        downloaded.save_to(str(dest))
        actual = _sha256_bytes(dest.read_bytes())
        if actual != entry.sha256:
            raise ChecksumMismatchError(
                artifact_id=artifact_id,
                expected_sha256=entry.sha256,
                actual_sha256=actual,
            )
        return dest

    def list_for_run(self, run_id: str) -> Manifest:
        with self._lock:
            order = self._run_order.get(run_id)
            if order is None:
                raise ManifestMissingError(run_id=run_id)
            entries = tuple(self._entries[aid] for aid in order)
        return Manifest(run_id=run_id, entries=entries)

    def exists(self, artifact_id: str) -> bool:
        with self._lock:
            return artifact_id in self._entries


__all__ = [
    "B2CheckpointStore",
    "InMemoryB2CheckpointStore",
    "LiveB2CheckpointStore",
]
