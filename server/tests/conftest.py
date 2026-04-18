"""Pytest configuration for the documentary server test suite.

Adds a ``--run-integration`` flag so network-hitting tests marked
``@pytest.mark.integration`` stay skipped unless explicitly requested.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run tests marked @pytest.mark.integration (network + API keys).",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: test hits real provider APIs; skipped unless --run-integration is passed.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-integration"):
        return
    skip_integration = pytest.mark.skip(
        reason="Needs --run-integration (network + provider API keys)."
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
