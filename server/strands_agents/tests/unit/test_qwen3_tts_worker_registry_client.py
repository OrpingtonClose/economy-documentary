"""Unit tests for the Qwen3-TTS worker's playground registry client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from strands_agents.qwen3_tts_worker.registry_client import (
    PlaygroundRegistryClient,
    RegistryClientError,
    RegistryHeartbeatError,
    RegistryRegisterError,
    RegistryUnregisterError,
    RegistryVoicePinError,
)


@dataclass(frozen=True)
class _Resp:
    status_code: int
    text: str = ""


@dataclass
class _FakeHttpPost:
    status_code: int = 200
    text: str = "{}"
    raise_exc: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(
        self, *, url: str, json: dict[str, object], timeout: float
    ) -> _Resp:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        return _Resp(status_code=self.status_code, text=self.text)


@dataclass
class _FakeHttpDelete:
    status_code: int = 200
    text: str = ""
    raise_exc: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, *, url: str, timeout: float) -> _Resp:
        self.calls.append({"url": url, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        return _Resp(status_code=self.status_code, text=self.text)


def test_register_success_returns_decoded_json() -> None:
    post = _FakeHttpPost(
        status_code=201,
        text=json.dumps({"worker_id": "w1", "role": "tts"}),
    )
    client = PlaygroundRegistryClient(
        base_url="https://playground.local/", http_post=post
    )
    payload = client.register(
        worker_id="w1",
        role="tts",
        endpoint_url="http://1.2.3.4:29231",
        vram_gb=24,
        voice_id="alex",
    )
    assert payload == {"worker_id": "w1", "role": "tts"}
    call = post.calls[0]
    assert call["url"] == "https://playground.local/playground/workers"
    assert call["json"] == {
        "worker_id": "w1",
        "role": "tts",
        "endpoint_url": "http://1.2.3.4:29231",
        "vram_gb": 24,
        "voice_id": "alex",
    }


def test_register_omits_voice_when_none() -> None:
    post = _FakeHttpPost(status_code=200, text="{}")
    client = PlaygroundRegistryClient(
        base_url="https://p.local", http_post=post
    )
    client.register(
        worker_id="w1",
        role="tts",
        endpoint_url="http://x",
        vram_gb=8,
    )
    body = post.calls[0]["json"]
    assert isinstance(body, dict)
    assert "voice_id" not in body


def test_register_non_2xx_raises() -> None:
    post = _FakeHttpPost(status_code=409, text='{"reason":"voice_already_pinned"}')
    client = PlaygroundRegistryClient(base_url="https://p", http_post=post)
    with pytest.raises(RegistryRegisterError):
        client.register(
            worker_id="w1",
            role="tts",
            endpoint_url="http://x",
            vram_gb=8,
            voice_id="dup",
        )


def test_register_network_error_wraps() -> None:
    post = _FakeHttpPost(raise_exc=RuntimeError("dns"))
    client = PlaygroundRegistryClient(base_url="https://p", http_post=post)
    with pytest.raises(RegistryClientError):
        client.register(
            worker_id="w1",
            role="tts",
            endpoint_url="http://x",
            vram_gb=8,
        )


def test_heartbeat_success() -> None:
    post = _FakeHttpPost(status_code=200, text=json.dumps({"ok": True}))
    client = PlaygroundRegistryClient(base_url="https://p", http_post=post)
    payload = client.heartbeat(worker_id="w1", free_vram_gb=16)
    assert payload == {"ok": True}
    call = post.calls[0]
    assert call["url"] == "https://p/playground/workers/w1/heartbeat"
    assert call["json"] == {"free_vram_gb": 16}


def test_heartbeat_omits_free_vram_when_none() -> None:
    post = _FakeHttpPost(status_code=200, text="{}")
    client = PlaygroundRegistryClient(base_url="https://p", http_post=post)
    client.heartbeat(worker_id="w1")
    assert post.calls[0]["json"] == {}


def test_heartbeat_non_2xx_raises() -> None:
    post = _FakeHttpPost(status_code=404)
    client = PlaygroundRegistryClient(base_url="https://p", http_post=post)
    with pytest.raises(RegistryHeartbeatError):
        client.heartbeat(worker_id="w1")


def test_pin_voice_success() -> None:
    post = _FakeHttpPost(
        status_code=200, text=json.dumps({"voice_id": "alex"})
    )
    client = PlaygroundRegistryClient(base_url="https://p", http_post=post)
    payload = client.pin_voice(worker_id="w1", voice_id="alex")
    assert payload == {"voice_id": "alex"}
    assert post.calls[0]["url"] == "https://p/playground/workers/w1/voice"


def test_pin_voice_conflict_raises() -> None:
    post = _FakeHttpPost(status_code=409)
    client = PlaygroundRegistryClient(base_url="https://p", http_post=post)
    with pytest.raises(RegistryVoicePinError):
        client.pin_voice(worker_id="w1", voice_id="alex")


def test_unregister_success() -> None:
    delete = _FakeHttpDelete(status_code=204)
    client = PlaygroundRegistryClient(
        base_url="https://p", http_delete=delete
    )
    client.unregister(worker_id="w1")
    assert delete.calls[0]["url"] == "https://p/playground/workers/w1"


def test_unregister_404_treated_as_success() -> None:
    delete = _FakeHttpDelete(status_code=404)
    client = PlaygroundRegistryClient(
        base_url="https://p", http_delete=delete
    )
    client.unregister(worker_id="w1")


def test_unregister_non_2xx_raises() -> None:
    delete = _FakeHttpDelete(status_code=500, text="boom")
    client = PlaygroundRegistryClient(
        base_url="https://p", http_delete=delete
    )
    with pytest.raises(RegistryUnregisterError):
        client.unregister(worker_id="w1")


def test_unregister_network_error_raises() -> None:
    delete = _FakeHttpDelete(raise_exc=RuntimeError("dns"))
    client = PlaygroundRegistryClient(
        base_url="https://p", http_delete=delete
    )
    with pytest.raises(RegistryUnregisterError):
        client.unregister(worker_id="w1")


def test_non_json_response_is_wrapped() -> None:
    post = _FakeHttpPost(status_code=200, text="not json")
    client = PlaygroundRegistryClient(base_url="https://p", http_post=post)
    payload = client.register(
        worker_id="w1",
        role="tts",
        endpoint_url="http://x",
        vram_gb=8,
    )
    assert payload == {"raw": "not json"}
