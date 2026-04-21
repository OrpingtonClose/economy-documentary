"""Pytest fixtures + CLI options for the Tier-2 suites.

The harness deliberately keeps hermetic mode cheap: fixtures just
hand back the seeded manifest + a seed-only fetcher, same as the
existing corpus tests.  Live mode is gated behind ``--tier2-live``
to keep every PR-gate run free of network / GPU costs.

Tests that want the live judge ensemble declare a dependency on
``tier2_judge_ensemble``.  The fixture skips the test when live
mode is off so the 15 suites share one import-and-fixture pattern.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from strands_agents.corpus.fetcher import CorpusFetcher, build_default_fetcher
from strands_agents.corpus.fixtures import load_default_manifest
from strands_agents.corpus.manifest import CorpusManifest

logger = logging.getLogger(__name__)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--tier2-live`` so nightly runs can wire the judge fleet."""
    group = parser.getgroup("tier2", "Tier-2 atomic-robustness evals")
    group.addoption(
        "--tier2-live",
        action="store_true",
        default=False,
        help=(
            "Run Tier-2 evals against a live JudgeEnsemble.  Requires the "
            "judge fleet to be reachable (Gemma-4 / Qwen3.5-Omni / "
            "video-SALMONN-2) or proprietary fallback creds.  Off by "
            "default so hermetic CI stays free."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers used by the Tier-2 suites."""
    config.addinivalue_line(
        "markers",
        "tier2_live: mark test as requiring the live judge fleet (skipped "
        "unless --tier2-live is passed)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip ``tier2_live`` tests when the live flag is off.

    This is more forgiving than requiring tests to opt in to the
    ``live_ensemble`` fixture — it lets suites parametrise freely and
    the collection modifier handles gating centrally.
    """
    if config.getoption("--tier2-live"):
        return
    skip_live = pytest.mark.skip(reason="requires --tier2-live")
    for item in items:
        if "tier2_live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def tier2_manifest() -> CorpusManifest:
    """Default corpus manifest used by every Tier-2 suite."""
    return load_default_manifest()


@pytest.fixture(scope="session")
def tier2_fetcher(tmp_path_factory: pytest.TempPathFactory) -> CorpusFetcher:
    """Session-scoped seed-only fetcher.

    Cache root is under ``tmp_path_factory`` so sessions don't collide
    and the cache is wiped between pytest runs — ground truth comes
    from the seed tree on every run.
    """
    cache_root: Path = tmp_path_factory.mktemp("tier2-corpus-cache")
    # Always hermetic at this fixture level.  Live judge is layered on
    # top of the same fetcher; artifact bytes are identical either way.
    return build_default_fetcher(cache_root=cache_root, enable_b2=False)


@pytest.fixture(scope="session")
def tier2_judge_ensemble(request: pytest.FixtureRequest) -> Any:
    """Construct a :class:`JudgeEnsemble` when ``--tier2-live`` is set.

    Skipped otherwise.  The import is deferred so this test package
    stays collectible even when the ``judges`` module hasn't been
    merged yet (PR-C is a parallel dependency).
    """
    if not request.config.getoption("--tier2-live"):
        pytest.skip("requires --tier2-live")

    try:
        from strands_agents.judges.ensemble import build_default_ensemble
    except ImportError as exc:
        pytest.skip(f"judges package unavailable: {exc}")

    return build_default_ensemble()
