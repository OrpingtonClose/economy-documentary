"""Unit tests for the LTX-Video worker registry client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from strands_agents.ltx_video_worker.registry_client import (
    PlaygroundRegistryClient,
    RegistryClientError,
    RegistryHeartbeatError,
    RegistryRegisterError,
    RegistryUnregisterError,
    _HttpResponse,
)


@dataclass
class _FakePost:
    status_code: int = 200
    text: str = "{}"
    calls: list[dict[str, Any]] = field(default_factory=list)
    exc: Exception | None = None

    def __call__(
        self, *, url: str, json: dict[str, object], timeout: float
    ) -> _HttpResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return _HttpResponse(status_code=self.status_code, text=self.text)


@dataclass
class _FakeDelete:
    status_code: int = 204
    text: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    exc: Exception | None = None

    def __call__(self, *, url: str, timeout: float) -> _HttpResponse:
        self.calls.append({"url": url, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return _HttpResponse(status_code=self.status_code, text=self.text)


def _client(
    *, post: _FakePost | None = None, delete: _FakeDelete | None = None
) -> PlaygroundRegistryClient:
    return PlaygroundRegistryClient(
        base_url="https://p.example/",  # trailing slash to exercise rstrip
        http_post=post or _FakePost(),
        http_delete=delete or _FakeDelete(),
    )


def test_register_success_returns_payload() -> None:
    post = _FakePost(status_code=200, text='{"worker_id":"w1"}')
    result = _client(post=post).register(
        worker_id="w1",
        role="video",
        endpoint_url="http://10.0.0.5:29232",
        vram_gb=141,
    )
    assert result == {"worker_id": "w1"}
    assert post.calls[0]["url"] == "https://p.example/playground/workers"
    assert post.calls[0]["json"] == {
        "worker_id": "w1",
        "role": "video",
        "endpoint_url": "http://10.0.0.5:29232",
        "vram_gb": 141,
    }


def test_register_sends_role_video() -> None:
    post = _FakePost()
    _client(post=post).register(
        worker_id="w", role="video", endpoint_url="u", vram_gb=48
    )
    assert post.calls[0]["json"]["role"] == "video"


def test_register_non_2xx_raises() -> None:
    post = _FakePost(status_code=409, text="voice conflict")
    with pytest.raises(RegistryRegisterError) as exc_info:
        _client(post=post).register(
            worker_id="w", role="video", endpoint_url="u", vram_gb=24
        )
    assert "409" in str(exc_info.value)


def test_register_network_error_wraps() -> None:
    post = _FakePost(exc=ConnectionError("dns"))
    with pytest.raises(RegistryClientError) as exc_info:
        _client(post=post).register(
            worker_id="w", role="video", endpoint_url="u", vram_gb=24
        )
    assert "dns" in str(exc_info.value)


def test_heartbeat_success_returns_payload() -> None:
    post = _FakePost(status_code=200, text='{"ok":true}')
    result = _client(post=post).heartbeat(worker_id="w1", free_vram_gb=100)
    assert result == {"ok": True}
    assert (
        post.calls[0]["url"]
        == "https://p.example/playground/workers/w1/heartbeat"
    )
    assert post.calls[0]["json"] == {"free_vram_gb": 100}


def test_heartbeat_omits_free_vram_when_none() -> None:
    post = _FakePost()
    _client(post=post).heartbeat(worker_id="w1", free_vram_gb=None)
    assert post.calls[0]["json"] == {}


def test_heartbeat_non_2xx_raises() -> None:
    post = _FakePost(status_code=503, text="boom")
    with pytest.raises(RegistryHeartbeatError):
        _client(post=post).heartbeat(worker_id="w1")


def test_heartbeat_network_error_wraps_as_base() -> None:
    post = _FakePost(exc=TimeoutError("slow"))
    with pytest.raises(RegistryClientError):
        _client(post=post).heartbeat(worker_id="w1")


def test_unregister_success_is_silent() -> None:
    delete = _FakeDelete(status_code=204)
    _client(delete=delete).unregister(worker_id="w1")
    assert delete.calls[0]["url"] == "https://p.example/playground/workers/w1"


def test_unregister_404_is_treated_as_success() -> None:
    delete = _FakeDelete(status_code=404, text="not found")
    _client(delete=delete).unregister(worker_id="w1")


def test_unregister_non_2xx_non_404_raises() -> None:
    delete = _FakeDelete(status_code=500, text="boom")
    with pytest.raises(RegistryUnregisterError):
        _client(delete=delete).unregister(worker_id="w1")


def test_unregister_network_error_wraps() -> None:
    delete = _FakeDelete(exc=ConnectionRefusedError("gone"))
    with pytest.raises(RegistryUnregisterError):
        _client(delete=delete).unregister(worker_id="w1")


def test_base_url_trailing_slash_is_normalised() -> None:
    post = _FakePost()
    PlaygroundRegistryClient(
        base_url="https://p.example///",
        http_post=post,
        http_delete=_FakeDelete(),
    ).register(worker_id="w", role="video", endpoint_url="u", vram_gb=1)
    assert post.calls[0]["url"] == "https://p.example/playground/workers"
