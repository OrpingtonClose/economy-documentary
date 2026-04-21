"""Corpus of real artifacts for per-component atomic-robustness evals.

The corpus is the ground truth the Tier-2 evals grade components against.
Each artifact carries metadata (component, role, expected verdict) so
evaluators can filter to the cases they care about and score against
the same ground truth every run.

Public API::

    from strands_agents.corpus import (
        CorpusArtifact,
        CorpusManifest,
        CorpusFetcher,
        load_default_manifest,
        build_default_fetcher,
    )

The default manifest is committed at
``strands_agents/corpus/default_manifest.json`` and references seed
files under ``strands_agents/corpus/seeds/``.  B2-backed artifacts
require ``B2_KEY_ID`` / ``B2_APPLICATION_KEY`` to be set.
"""

from __future__ import annotations

from strands_agents.corpus.cache import (
    DEFAULT_CACHE_ROOT,
    compute_sha256,
    path_for,
    resolve_cache_root,
    store,
    verify_bytes,
)
from strands_agents.corpus.fetcher import (
    B2CorpusBackend,
    CorpusBackend,
    CorpusFetcher,
    MockBackend,
    SeedBackend,
    build_default_fetcher,
)
from strands_agents.corpus.fixtures import (
    DEFAULT_MANIFEST_PATH,
    filter_artifacts,
    load_artifact_bytes,
    load_artifact_json,
    load_default_manifest,
)
from strands_agents.corpus.manifest import (
    CorpusArtifact,
    CorpusComponent,
    CorpusContentType,
    CorpusManifest,
    CorpusRole,
    CorpusStorage,
    load_manifest,
)

__all__ = [
    "B2CorpusBackend",
    "CorpusArtifact",
    "CorpusBackend",
    "CorpusComponent",
    "CorpusContentType",
    "CorpusFetcher",
    "CorpusManifest",
    "CorpusRole",
    "CorpusStorage",
    "DEFAULT_CACHE_ROOT",
    "DEFAULT_MANIFEST_PATH",
    "MockBackend",
    "SeedBackend",
    "build_default_fetcher",
    "compute_sha256",
    "filter_artifacts",
    "load_artifact_bytes",
    "load_artifact_json",
    "load_default_manifest",
    "load_manifest",
    "path_for",
    "resolve_cache_root",
    "store",
    "verify_bytes",
]
