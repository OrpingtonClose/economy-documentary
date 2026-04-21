"""Unit tests for :mod:`strands_agents.evals.providers.langfuse`.

The provider must not crash on import when the ``langfuse`` extra is
absent. It must raise :class:`LangfuseUnavailableError` only at call
time, with the missing env vars surfaced in the message so CI can
write a meaningful failure log.
"""

from __future__ import annotations

import pytest

from strands_agents.evals.providers.langfuse import (
    LangfuseUnavailableError,
    get_langfuse_provider,
    is_langfuse_available,
)

_ENV_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")


def test_missing_env_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    assert is_langfuse_available() is False


def test_missing_env_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(LangfuseUnavailableError) as exc:
        get_langfuse_provider()
    assert "LANGFUSE_PUBLIC_KEY" in str(exc.value)
    assert "LANGFUSE_SECRET_KEY" in str(exc.value)


def test_partial_env_still_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert is_langfuse_available() is False
    with pytest.raises(LangfuseUnavailableError) as exc:
        get_langfuse_provider()
    assert "LANGFUSE_SECRET_KEY" in str(exc.value)


def test_with_env_but_no_package_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Env is present, but the ``langfuse`` module is not installed in
    # this environment. Import-time failure should be wrapped, not
    # propagated, so callers can fall back cleanly.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    pytest.importorskip_missing = True  # cosmetic — document intent
    try:
        import langfuse  # noqa: F401
    except ImportError:
        with pytest.raises(LangfuseUnavailableError) as exc:
            get_langfuse_provider()
        assert "import_error" in str(exc.value)
    else:
        # Package is installed; this path is exercised by integration
        # tests, not unit tests.
        pytest.skip("`langfuse` is installed; covered by integration tests")
