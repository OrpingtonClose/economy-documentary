"""Unit tests for the infra-agent bump client."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pytest

from strands_agents.qwen3_tts_worker.bump_client import (
    InfraAgentBumpClient,
    bump_infra_agent,
)


@dataclass
class _FakeHttpPost:
    status_code: int = 200
    raise_exc: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, *, url: str, timeout: float) -> object:
        self.calls.append({"url": url, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc

        @dataclass(frozen=True)
        class _R:
            status_code: int

        return _R(status_code=self.status_code)


def test_bump_ok_calls_configured_url() -> None:
    post = _FakeHttpPost(status_code=204)
    client = InfraAgentBumpClient(
        url="http://127.0.0.1:29230/infra/bump",
        http_post=post,
        timeout_s=1.5,
    )
    client.bump()
    assert post.calls == [
        {"url": "http://127.0.0.1:29230/infra/bump", "timeout": 1.5}
    ]


def test_bump_swallows_network_error(caplog: pytest.LogCaptureFixture) -> None:
    post = _FakeHttpPost(raise_exc=RuntimeError("boom"))
    client = InfraAgentBumpClient(http_post=post)
    with caplog.at_level(logging.WARNING):
        client.bump()
    assert any("network error" in rec.getMessage() for rec in caplog.records)


def test_bump_swallows_non_2xx(caplog: pytest.LogCaptureFixture) -> None:
    post = _FakeHttpPost(status_code=500)
    client = InfraAgentBumpClient(http_post=post)
    with caplog.at_level(logging.WARNING):
        client.bump()
    assert any("non-2xx" in rec.getMessage() for rec in caplog.records)


def test_bump_infra_agent_shortcut() -> None:
    post = _FakeHttpPost(status_code=200)
    bump_infra_agent(url="http://localhost:29230/infra/bump", http_post=post)
    assert len(post.calls) == 1
