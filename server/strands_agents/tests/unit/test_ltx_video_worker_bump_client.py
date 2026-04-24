"""Unit tests for the LTX-Video worker's infra-agent bump client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strands_agents.ltx_video_worker.bump_client import (
    DEFAULT_BUMP_URL,
    InfraAgentBumpClient,
    bump_infra_agent,
)


@dataclass
class _FakePost:
    status_code: int = 200
    calls: list[dict[str, Any]] = field(default_factory=list)
    exc: Exception | None = None

    def __call__(self, *, url: str, timeout: float) -> object:
        self.calls.append({"url": url, "timeout": timeout})
        if self.exc is not None:
            raise self.exc

        @dataclass(frozen=True)
        class _R:
            status_code: int

        return _R(status_code=self.status_code)


def test_bump_client_default_url() -> None:
    assert InfraAgentBumpClient().url == DEFAULT_BUMP_URL


def test_bump_success_invokes_post_once() -> None:
    post = _FakePost(status_code=200)
    client = InfraAgentBumpClient(url="http://x:29230/infra/bump", http_post=post)
    client.bump()
    assert post.calls == [
        {"url": "http://x:29230/infra/bump", "timeout": 2.0}
    ]


def test_bump_swallows_network_error() -> None:
    post = _FakePost(exc=ConnectionRefusedError("down"))
    client = InfraAgentBumpClient(http_post=post)
    client.bump()  # must not raise


def test_bump_swallows_non_2xx() -> None:
    post = _FakePost(status_code=503)
    client = InfraAgentBumpClient(http_post=post)
    client.bump()  # must not raise
    assert len(post.calls) == 1


def test_bump_swallows_5xx() -> None:
    post = _FakePost(status_code=500)
    InfraAgentBumpClient(http_post=post).bump()


def test_bump_honours_custom_timeout() -> None:
    post = _FakePost()
    InfraAgentBumpClient(http_post=post, timeout_s=0.5).bump()
    assert post.calls[0]["timeout"] == 0.5


def test_bump_infra_agent_shortcut() -> None:
    post = _FakePost()
    bump_infra_agent(url="http://h:1/infra/bump", http_post=post, timeout_s=1.25)
    assert post.calls == [{"url": "http://h:1/infra/bump", "timeout": 1.25}]


def test_bump_infra_agent_shortcut_swallows_errors() -> None:
    post = _FakePost(exc=TimeoutError("slow"))
    bump_infra_agent(http_post=post)
