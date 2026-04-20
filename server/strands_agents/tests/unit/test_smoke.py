"""Phase-0 smoke tests.

The bar is intentionally low: confirm the package imports, the three
SDKs install cleanly, and the spec docs referenced by every future
component actually exist. A real Experiment runs in component 1
(eval-framework).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_package_imports() -> None:
    """The package and its subpackages import without side effects."""
    import strands_agents as pkg

    assert pkg.__version__ == "0.0.1"

    for submodule in (
        "strands_agents.tools",
        "strands_agents.subagents",
        "strands_agents.leaves",
        "strands_agents.evals",
        "strands_agents.evals.evaluators",
        "strands_agents.evals.simulators",
    ):
        importlib.import_module(submodule)


def test_sdk_dependencies_installable() -> None:
    """The three pinned SDKs import. Version floors are resolved via
    ``importlib.metadata`` because ``strands`` and ``strands_evals`` do
    not expose ``__version__`` as a module attribute; the exact pin
    lives in ``pyproject.toml``.
    """
    from importlib.metadata import version

    import deepagents  # noqa: F401
    import strands  # noqa: F401
    import strands_evals  # noqa: F401

    assert version("deepagents") == "0.5.3"
    assert version("strands-agents") == "1.36.0"
    assert version("strands-agents-evals") == "0.1.15"


@pytest.mark.parametrize(
    "relative_path",
    [
        "docs/strands-migration/README.md",
        "docs/strands-migration/AGENTS.md",
        "docs/strands-migration/SEQUENCE.md",
        "docs/strands-migration/ARCHITECTURE.md",
        "docs/strands-migration/eval-framework/THRESHOLDS.md",
        "docs/strands-migration/reference/DEEPAGENT_PATTERNS.md",
    ],
)
def test_referenced_spec_files_exist(relative_path: str) -> None:
    """Every downstream component PR references these spec files. Fail
    fast if one is moved or deleted before its dependents migrate.
    """
    repo_root = Path(__file__).resolve().parents[4]
    assert (repo_root / relative_path).is_file(), (
        f"Missing spec file: {relative_path}. Downstream components depend on it."
    )
