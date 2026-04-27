"""Direct-proof tests for :class:`LiveB2CheckpointStore`.

Covers the TOCTOU fix and the stale-revision / idempotency-collision
guards ported from :class:`InMemoryB2CheckpointStore`. The b2sdk client
is replaced with a fake bucket so no real B2 traffic flows.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from strands_agents.b2_checkpoint.errors import (
    DuplicateIdempotencyKeyError,
    StaleRevisionError,
)
from strands_agents.b2_checkpoint.store import LiveB2CheckpointStore


class _FakeBucket:
    """In-memory stand-in for the ``b2sdk.v2`` bucket handle.

    Records every ``upload_bytes`` call so tests can assert how many
    times the network surface was hit.
    """

    def __init__(self, *, upload_delay_s: float = 0.0) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self._upload_delay_s = upload_delay_s
        self._lock = threading.Lock()

    def upload_bytes(self, *, data_bytes: bytes, file_name: str) -> object:
        if self._upload_delay_s > 0:
            time.sleep(self._upload_delay_s)
        with self._lock:
            self.uploads.append((file_name, data_bytes))
        return object()


def _make_store(bucket: _FakeBucket | None = None) -> LiveB2CheckpointStore:
    store = LiveB2CheckpointStore.__new__(LiveB2CheckpointStore)
    store._bucket_name = "test-bucket"
    store._key_id = "fake-key"
    store._application_key = "fake-secret"
    store._client = object()
    store._bucket = bucket if bucket is not None else _FakeBucket()
    store._lock = threading.Lock()
    store._entries = {}
    store._by_idem = {}
    store._run_order = {}
    store._in_flight = {}
    return store


class TestLiveB2CheckpointStoreUpload:
    def test_uploads_payload_and_records_manifest(self) -> None:
        bucket = _FakeBucket()
        store = _make_store(bucket)

        entry = store.upload(
            payload=b"hello",
            kind="audio_wav",
            revision_tag="r0001",
            run_id="run-1",
        )

        assert entry.run_id == "run-1"
        assert entry.kind == "audio_wav"
        assert entry.revision_tag == "r0001"
        assert entry.size_bytes == len(b"hello")
        assert len(bucket.uploads) == 1
        assert bucket.uploads[0][1] == b"hello"

    def test_idempotent_replay_short_circuits(self) -> None:
        bucket = _FakeBucket()
        store = _make_store(bucket)

        first = store.upload(
            payload=b"hello",
            kind="audio_wav",
            revision_tag="r0001",
            run_id="run-1",
        )
        second = store.upload(
            payload=b"hello",
            kind="audio_wav",
            revision_tag="r0001",
            run_id="run-1",
        )

        assert first.artifact_id == second.artifact_id
        # Second call must not re-upload.
        assert len(bucket.uploads) == 1


class TestLiveB2CheckpointStoreStaleRevision:
    def test_uploading_older_revision_after_newer_raises(self) -> None:
        store = _make_store()

        store.upload(
            payload=b"v2-bytes",
            kind="scene_json",
            revision_tag="r0002",
            run_id="run-1",
        )

        with pytest.raises(StaleRevisionError) as excinfo:
            store.upload(
                payload=b"v1-bytes",
                kind="scene_json",
                revision_tag="r0001",
                run_id="run-1",
            )

        assert excinfo.value.attempted_revision_tag == "r0001"
        assert excinfo.value.latest_revision_tag == "r0002"

    def test_stale_revision_releases_in_flight_slot(self) -> None:
        """Failed claim must not leak ``_in_flight`` entries."""
        store = _make_store()

        store.upload(
            payload=b"v2-bytes",
            kind="scene_json",
            revision_tag="r0002",
            run_id="run-1",
        )
        with pytest.raises(StaleRevisionError):
            store.upload(
                payload=b"v1-bytes",
                kind="scene_json",
                revision_tag="r0001",
                run_id="run-1",
            )

        assert store._in_flight == {}

    def test_same_revision_not_stale(self) -> None:
        store = _make_store()

        first = store.upload(
            payload=b"a",
            kind="audio_wav",
            revision_tag="r0001",
            run_id="run-1",
        )
        second = store.upload(
            payload=b"b",
            kind="audio_wav",
            revision_tag="r0001",
            run_id="run-1",
        )

        assert first.artifact_id != second.artifact_id

    def test_different_run_ids_independent(self) -> None:
        store = _make_store()

        store.upload(
            payload=b"a",
            kind="audio_wav",
            revision_tag="r0005",
            run_id="run-A",
        )
        # Older revision in a different run must not be flagged stale.
        entry = store.upload(
            payload=b"b",
            kind="audio_wav",
            revision_tag="r0001",
            run_id="run-B",
        )
        assert entry.revision_tag == "r0001"


class TestLiveB2CheckpointStoreToctouRace:
    def test_concurrent_uploads_with_same_idem_coalesce(self) -> None:
        """Two threads, identical bytes → one B2 upload, one manifest entry."""
        bucket = _FakeBucket(upload_delay_s=0.05)
        store = _make_store(bucket)

        ready = threading.Barrier(2)

        def _upload() -> str:
            ready.wait()
            entry = store.upload(
                payload=b"identical-bytes",
                kind="audio_wav",
                revision_tag="r0001",
                run_id="run-1",
            )
            return entry.artifact_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(_upload) for _ in range(2)]
            results = [f.result() for f in futures]

        assert results[0] == results[1], "Concurrent callers must converge"
        # The orphan-discard branch may run a second upload, but the
        # second caller's manifest entry is discarded so only one entry
        # ends up in the store. The fast-path branch (winner already
        # registered) avoids the second upload entirely.
        assert len(store._entries) == 1
        assert len(store._run_order["run-1"]) == 1
        assert store._in_flight == {}

    def test_concurrent_distinct_payloads_both_succeed(self) -> None:
        """Distinct sha256 → distinct idem → no coalescing."""
        bucket = _FakeBucket(upload_delay_s=0.02)
        store = _make_store(bucket)

        ready = threading.Barrier(2)

        def _upload(payload: bytes) -> str:
            ready.wait()
            entry = store.upload(
                payload=payload,
                kind="audio_wav",
                revision_tag="r0001",
                run_id="run-1",
            )
            return entry.artifact_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(_upload, b"payload-A"),
                executor.submit(_upload, b"payload-B"),
            ]
            results = [f.result() for f in futures]

        assert results[0] != results[1]
        assert len(bucket.uploads) == 2
        assert len(store._entries) == 2
        assert store._in_flight == {}


class TestLiveB2CheckpointStoreUploadFailure:
    def test_upload_failure_releases_in_flight_slot(self) -> None:
        """A failed B2 upload must not leak the in-flight claim."""

        class _FailingBucket(_FakeBucket):
            def upload_bytes(
                self, *, data_bytes: bytes, file_name: str
            ) -> object:
                raise RuntimeError("simulated b2 failure")

        bucket = _FailingBucket()
        store = _make_store(bucket)

        with pytest.raises(RuntimeError, match="simulated b2 failure"):
            store.upload(
                payload=b"hello",
                kind="audio_wav",
                revision_tag="r0001",
                run_id="run-1",
            )

        assert store._in_flight == {}
        # A retry with the same bytes must succeed (slot was released).
        store._bucket = _FakeBucket()
        entry = store.upload(
            payload=b"hello",
            kind="audio_wav",
            revision_tag="r0001",
            run_id="run-1",
        )
        assert entry.size_bytes == len(b"hello")


class TestLiveB2CheckpointStoreDuplicateIdempotencyKey:
    def test_collision_with_different_sha_raises(self) -> None:
        """Pre-computed idem key with different bytes → caller bug."""
        store = _make_store()

        first = store.upload(
            payload=b"original",
            kind="audio_wav",
            revision_tag="r0001",
            run_id="run-1",
        )

        # The collision guard only fires when ``_entries`` and
        # ``_by_idem`` are out of sync — otherwise the ``_by_idem``
        # fast-path returns the existing entry first. Simulate the
        # corruption: drop the ``_by_idem`` mapping, mutate the stored
        # sha256, and attempt to upload with the original idem-key but
        # incoming bytes that match the original sha256.
        synthetic_idem = first.idempotency_key
        del store._by_idem[synthetic_idem]
        store._entries[first.artifact_id] = first.__class__(
            artifact_id=first.artifact_id,
            run_id=first.run_id,
            revision_tag=first.revision_tag,
            kind=first.kind,
            b2_key=first.b2_key,
            sha256="0" * 64,
            size_bytes=first.size_bytes,
            uploaded_at_iso=first.uploaded_at_iso,
            idempotency_key=synthetic_idem,
        )

        with pytest.raises(DuplicateIdempotencyKeyError):
            store.upload(
                payload=b"original",
                kind="audio_wav",
                revision_tag="r0001",
                run_id="run-1",
            )

        assert store._in_flight == {}
