"""Shared pytest hooks for the live-judge proof-of-life suite.

Tests under this directory hit real cloud APIs (Google, Alibaba, Anthropic).
They are never skipped for flakiness — if credentials are present in the
environment, the tests run and must pass every time, in line with the
"must pass ALL THE TIME" judge policy.  If credentials are absent, the
tests skip cleanly so the hermetic CI still passes.

This conftest centralises the credential-presence skip logic so
individual tests don't repeat the same ``skipif`` boilerplate.
"""

from __future__ import annotations

import os

import pytest


def _has(env: str) -> bool:
    """Return True iff ``env`` is set and non-empty in ``os.environ``."""
    return bool(os.environ.get(env, "").strip())


requires_google_api = pytest.mark.skipif(
    not _has("GOOGLE_API_KEY"),
    reason="GOOGLE_API_KEY not set; skipping live Gemini call.",
)
requires_dashscope_api = pytest.mark.skipif(
    not (_has("DASHSCOPE_API_KEY") or _has("DASHSCOPE_INTL_API_KEY")),
    reason="DASHSCOPE_API_KEY / DASHSCOPE_INTL_API_KEY not set; skipping live Qwen call.",
)
requires_anthropic_api = pytest.mark.skipif(
    not _has("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set; skipping live Claude call.",
)
