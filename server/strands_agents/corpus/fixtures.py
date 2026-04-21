"""Pytest fixtures + loaders for the corpus.

Tier-2 eval suites use these helpers to pull real artifacts into a
test.  The common pattern is::

    def test_scenario_rejects_offtopic(corpus_fetcher, corpus_manifest):
        artifact = corpus_manifest.get("scenario.adversarial.offtopic")
        path = corpus_fetcher.resolve(artifact)
        scenario = json.loads(path.read_text())
        ...

For hermetic unit tests we provide a ``corpus_fetcher`` that serves
seed-committed artifacts only (no B2).  For live Tier-2 runs the
``--corpus-live`` pytest CLI option attaches a B2 backend.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from strands_agents.corpus.fetcher import CorpusFetcher, build_default_fetcher
from strands_agents.corpus.manifest import (
    CorpusArtifact,
    CorpusComponent,
    CorpusManifest,
    CorpusRole,
    load_manifest,
)

logger = logging.getLogger(__name__)

# The default manifest shipped with the repo.  Tests that want a custom
# manifest should point to their own file explicitly.
DEFAULT_MANIFEST_PATH = Path(__file__).parent / "default_manifest.json"


def load_default_manifest() -> CorpusManifest:
    """Load the manifest committed in the repo."""
    return load_manifest(DEFAULT_MANIFEST_PATH)


def load_artifact_bytes(
    fetcher: CorpusFetcher,
    artifact: CorpusArtifact,
) -> bytes:
    """Resolve ``artifact`` and return its raw bytes."""
    return fetcher.resolve(artifact).read_bytes()


def load_artifact_json(
    fetcher: CorpusFetcher,
    artifact: CorpusArtifact,
) -> object:
    """Resolve ``artifact`` and parse it as JSON.

    Raises:
        ValueError: If the artifact's content type is not a JSON flavour.
    """
    json_types = {
        "scenario_json",
        "timing_report_json",
        "critique_json",
        "escalation_decision_json",
    }
    if artifact.content_type not in json_types:
        raise ValueError(
            f"artifact key=<{artifact.key}> content_type=<{artifact.content_type}> "
            f"is not JSON"
        )
    path = fetcher.resolve(artifact)
    return json.loads(path.read_text())


def filter_artifacts(
    manifest: CorpusManifest,
    *,
    component: Optional[CorpusComponent] = None,
    role: Optional[CorpusRole] = None,
) -> tuple[CorpusArtifact, ...]:
    """Return artifacts matching all non-None filter args.

    No filters → all artifacts.  Combine freely for pytest
    parametrisation, e.g.::

        pytest.mark.parametrize(
            "artifact",
            filter_artifacts(manifest, component="01-scenario-agent",
                             role="adversarial"),
        )
    """
    results = manifest.artifacts
    if component is not None:
        results = tuple(a for a in results if a.component == component)
    if role is not None:
        results = tuple(a for a in results if a.role == role)
    return results


# -- pytest plugin entry points --------------------------------------------
# These are importable via ``pytest_plugins = ["strands_agents.corpus.fixtures"]``
# in a test module's ``conftest.py``.  We deliberately avoid auto-loading
# so hermetic test files that never touch the corpus don't pay the cost
# of instantiating a fetcher.


def pytest_addoption(parser: object) -> None:  # pragma: no cover - pytest hook
    """Register ``--corpus-live`` CLI option.

    When set, ``corpus_fetcher`` attaches a B2 backend so tests can
    resolve ``storage="b2"`` artifacts.  Default (unset) is hermetic
    seed-only mode.
    """
    parser.addoption(  # type: ignore[attr-defined]
        "--corpus-live",
        action="store_true",
        default=False,
        help="Enable live B2 fetches for corpus artifacts (requires credentials)",
    )


try:
    import pytest
except ImportError:  # pragma: no cover - pytest is required for fixtures
    pass
else:
    @pytest.fixture(scope="session")
    def corpus_manifest() -> CorpusManifest:
        """Session-scoped fixture: the default corpus manifest."""
        return load_default_manifest()

    @pytest.fixture(scope="session")
    def corpus_fetcher(
        request: pytest.FixtureRequest,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> CorpusFetcher:
        """Session-scoped fixture: a :class:`CorpusFetcher`.

        Hermetic by default (seed-backend only, cache under a
        session-scoped temp dir).  Pass ``--corpus-live`` to attach a
        B2 backend for Tier-2 live runs.
        """
        cache_root = tmp_path_factory.mktemp("corpus-cache")
        live = bool(request.config.getoption("--corpus-live", default=False))
        return build_default_fetcher(cache_root=cache_root, enable_b2=live)
