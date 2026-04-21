"""Corpus fetcher — resolve an artifact to a local file path.

The fetcher is the read-side of the corpus: given a
:class:`CorpusArtifact`, return a local path whose bytes match the
artifact's sha256.  It mediates between three storage backends:

- **seed**: bytes are committed under ``corpus/seeds/`` in the repo.
  Hermetic; used for unit tests and CI without B2 access.
- **b2**: bytes live in the private B2 bucket.  Fetch via
  :class:`B2CorpusBackend` which lazily imports :mod:`b2sdk` and reuses
  the existing ``B2_KEY_ID`` / ``B2_APPLICATION_KEY`` env vars.
- **mock**: in-memory bytes keyed by sha256.  Used by tests that need
  to exercise the fetch path without touching B2.

The fetcher caches every successful fetch into the content-addressed
cache so subsequent runs skip the network.  Cache hits are verified —
a mismatch is treated as corruption and re-fetched.
"""

from __future__ import annotations

import logging
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from strands_agents.corpus import cache
from strands_agents.corpus.manifest import CorpusArtifact

logger = logging.getLogger(__name__)


class CorpusBackend(ABC):
    """Abstract backend — knows how to materialise an artifact's bytes."""

    @abstractmethod
    def fetch(self, artifact: CorpusArtifact, dest: Path) -> None:
        """Write the artifact's bytes to ``dest``.

        Implementations MUST raise on any failure; the caller verifies
        the sha256 after return.
        """


class SeedBackend(CorpusBackend):
    """Backend that reads committed seed files from the repo.

    Seed paths in the manifest are repo-relative.  The backend resolves
    them against :attr:`seed_root` (by default, ``corpus/seeds/`` next
    to this module).
    """

    def __init__(self, seed_root: Optional[Path] = None) -> None:
        """Create a seed backend rooted at ``seed_root``.

        Args:
            seed_root: Filesystem root to resolve seed paths against.
                Defaults to the ``seeds/`` directory colocated with
                this module.
        """
        if seed_root is None:
            seed_root = Path(__file__).parent / "seeds"
        self._root = seed_root.resolve()

    def fetch(self, artifact: CorpusArtifact, dest: Path) -> None:
        """Copy the seed file into ``dest``."""
        if artifact.seed_path is None:
            raise ValueError(
                f"artifact key=<{artifact.key}> has no seed_path"
            )
        source = (self._root / artifact.seed_path).resolve()
        try:
            source.relative_to(self._root)
        except ValueError as exc:
            # Defence in depth — a manifest entry with ``../`` in its
            # seed_path shouldn't be able to read files outside the
            # committed seeds directory.
            raise ValueError(
                f"artifact key=<{artifact.key}> seed_path escapes root"
            ) from exc
        if not source.exists():
            raise FileNotFoundError(
                f"seed path=<{source}> for artifact key=<{artifact.key}> "
                f"does not exist"
            )
        dest.write_bytes(source.read_bytes())


class MockBackend(CorpusBackend):
    """Backend that serves pre-loaded bytes keyed by sha256.

    Tests populate the :attr:`blobs` dict and then point the fetcher at
    this backend to exercise cache + hash-verify paths without hitting
    B2 or the filesystem.
    """

    def __init__(self, blobs: Optional[dict[str, bytes]] = None) -> None:
        """Create a mock backend with optional initial blobs."""
        self.blobs: dict[str, bytes] = dict(blobs or {})
        self.fetch_calls: list[str] = []

    def put(self, sha256: str, data: bytes) -> None:
        """Register ``data`` as the bytes for ``sha256``."""
        self.blobs[sha256] = data

    def fetch(self, artifact: CorpusArtifact, dest: Path) -> None:
        """Write the registered blob for ``artifact.sha256`` to ``dest``.

        Raises:
            KeyError: If no blob is registered for the artifact.
        """
        self.fetch_calls.append(artifact.key)
        if artifact.sha256 not in self.blobs:
            raise KeyError(
                f"mock backend has no bytes for sha256=<{artifact.sha256}>"
            )
        dest.write_bytes(self.blobs[artifact.sha256])


class B2CorpusBackend(CorpusBackend):
    """Backend that downloads artifacts from a B2 bucket.

    Lazily imports ``b2sdk`` so the module can be imported in hermetic
    environments (CI without B2 creds).  The bucket handle is cached
    across calls.
    """

    def __init__(
        self,
        *,
        bucket_name: Optional[str] = None,
        key_id: Optional[str] = None,
        application_key: Optional[str] = None,
    ) -> None:
        """Create a B2 backend bound to ``bucket_name``.

        Args:
            bucket_name: B2 bucket holding corpus artifacts.  Falls
                back to ``STRANDS_CORPUS_B2_BUCKET`` env var.
            key_id: B2 key ID.  Falls back to ``B2_KEY_ID`` env var.
            application_key: B2 application key.  Falls back to
                ``B2_APPLICATION_KEY``.
        """
        self._bucket_name = bucket_name or os.environ.get(
            "STRANDS_CORPUS_B2_BUCKET", "cloudberry-documentary-v2",
        )
        self._key_id = key_id or os.environ.get("B2_KEY_ID", "")
        self._application_key = application_key or os.environ.get(
            "B2_APPLICATION_KEY", "",
        )
        self._bucket: Any = None

    def _get_bucket(self) -> Any:
        """Lazily authorise and return the B2 bucket handle."""
        if self._bucket is not None:
            return self._bucket
        if not self._key_id or not self._application_key:
            raise RuntimeError(
                "B2_KEY_ID / B2_APPLICATION_KEY not set — cannot fetch "
                "corpus artifacts from B2"
            )
        from b2sdk.v2 import B2Api, InMemoryAccountInfo

        info = InMemoryAccountInfo()
        api = B2Api(info)
        api.authorize_account("production", self._key_id, self._application_key)
        self._bucket = api.get_bucket_by_name(self._bucket_name)
        logger.info(
            "bucket=<%s> | authorized b2 corpus backend",
            self._bucket_name,
        )
        return self._bucket

    def fetch(self, artifact: CorpusArtifact, dest: Path) -> None:
        """Download the artifact's bytes from B2 into ``dest``."""
        if artifact.b2_key is None:
            raise ValueError(
                f"artifact key=<{artifact.key}> has no b2_key"
            )
        bucket = self._get_bucket()
        downloaded = bucket.download_file_by_name(artifact.b2_key)
        downloaded.save_to(str(dest))


class CorpusFetcher:
    """Resolve corpus artifacts to local file paths, with caching.

    The fetcher is configured with one backend per storage scheme.  An
    artifact's :attr:`CorpusArtifact.storage` field selects the
    backend.  Results are cached in the content-addressed local cache.
    """

    def __init__(
        self,
        *,
        cache_root: Optional[Path | str] = None,
        seed_backend: Optional[CorpusBackend] = None,
        b2_backend: Optional[CorpusBackend] = None,
        mock_backend: Optional[CorpusBackend] = None,
    ) -> None:
        """Create a fetcher with per-storage backends.

        Args:
            cache_root: Explicit cache root.  Falls back to env var
                ``STRANDS_CORPUS_CACHE`` and then
                :data:`~strands_agents.corpus.cache.DEFAULT_CACHE_ROOT`.
            seed_backend: Backend for ``storage="seed"`` artifacts.
                Defaults to :class:`SeedBackend` rooted at the
                committed seeds dir.
            b2_backend: Backend for ``storage="b2"`` artifacts.  Not
                constructed by default — callers opting into B2 must
                explicitly pass a :class:`B2CorpusBackend`.
            mock_backend: Optional backend used for test-registered
                artifacts.  When present, takes precedence over the
                storage-keyed dispatch, which lets unit tests redirect
                any artifact to pre-loaded bytes.
        """
        self._cache_root = cache.resolve_cache_root(cache_root)
        self._seed = seed_backend or SeedBackend()
        self._b2 = b2_backend
        self._mock = mock_backend

    @property
    def cache_root(self) -> Path:
        """Absolute path to the cache root in use."""
        return self._cache_root

    def has_b2(self) -> bool:
        """True iff a B2 backend is wired up for this fetcher."""
        return self._b2 is not None

    def resolve(self, artifact: CorpusArtifact) -> Path:
        """Return a local path whose bytes match ``artifact.sha256``.

        Fetches from the appropriate backend on cache miss, then
        verifies the sha256 before returning.  Mismatches trigger one
        re-fetch; a second mismatch raises :class:`ValueError` because
        that indicates a backend bug, not cache corruption.
        """
        cached = cache.path_for(self._cache_root, artifact.sha256)
        if cache.verify_bytes(cached, artifact.sha256):
            logger.debug(
                "key=<%s>, path=<%s> | corpus cache hit",
                artifact.key, cached,
            )
            return cached

        backend = self._select_backend(artifact)
        self._fetch_into_cache(artifact, backend)

        cached = cache.path_for(self._cache_root, artifact.sha256)
        if not cache.verify_bytes(cached, artifact.sha256):
            raise ValueError(
                f"artifact key=<{artifact.key}> hash mismatch after "
                f"fetch via backend=<{type(backend).__name__}>"
            )
        return cached

    def _select_backend(self, artifact: CorpusArtifact) -> CorpusBackend:
        """Pick the backend that should serve ``artifact``."""
        if self._mock is not None and isinstance(self._mock, MockBackend):
            # Prefer mock when the test has pre-registered this sha.
            if artifact.sha256 in self._mock.blobs:
                return self._mock
        if artifact.storage == "seed":
            return self._seed
        if artifact.storage == "b2":
            if self._b2 is None:
                raise RuntimeError(
                    f"artifact key=<{artifact.key}> requires b2 backend "
                    f"but none is configured"
                )
            return self._b2
        raise ValueError(
            f"artifact key=<{artifact.key}> has unknown storage=<{artifact.storage}>"
        )

    def _fetch_into_cache(
        self,
        artifact: CorpusArtifact,
        backend: CorpusBackend,
    ) -> None:
        """Invoke ``backend.fetch`` and atomically install the result."""
        with tempfile.NamedTemporaryFile(
            dir=self._cache_root, delete=False,
            prefix=".corpus.", suffix=".download",
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            backend.fetch(artifact, tmp_path)
            cache.store(self._cache_root, artifact.sha256, tmp_path, copy=False)
        finally:
            tmp_path.unlink(missing_ok=True)


def build_default_fetcher(
    *,
    cache_root: Optional[Path | str] = None,
    enable_b2: Optional[bool] = None,
) -> CorpusFetcher:
    """Construct a :class:`CorpusFetcher` with production defaults.

    Args:
        cache_root: Explicit cache root (see :class:`CorpusFetcher`).
        enable_b2: Whether to attach a :class:`B2CorpusBackend`.  If
            ``None`` (default), auto-enables when both ``B2_KEY_ID``
            and ``B2_APPLICATION_KEY`` are set.

    Returns:
        A fetcher with a seed backend always, plus B2 when credentials
        are present.
    """
    if enable_b2 is None:
        enable_b2 = bool(os.environ.get("B2_KEY_ID")) and bool(
            os.environ.get("B2_APPLICATION_KEY"),
        )
    b2_backend = B2CorpusBackend() if enable_b2 else None
    return CorpusFetcher(cache_root=cache_root, b2_backend=b2_backend)
