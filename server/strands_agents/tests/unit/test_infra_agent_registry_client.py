"""Unit tests for the playground registry deregister client."""

from __future__ import annotations

import pytest

from strands_agents.infra_agent.registry_client import (
    PlaygroundRegistryClient,
    RegistryDeregisterError,
    _HttpResponse,
)


class _DeleteCall:
    def __init__(self, response: _HttpResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def __call__(self, *, url: str, timeout: float) -> _HttpResponse:
        self.calls.append({"url": url, "timeout": timeout})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_deregister_happy_path_hits_expected_url() -> None:
    stub = _DeleteCall(_HttpResponse(status_code=204, text=""))
    client = PlaygroundRegistryClient(
        base_url="https://playground.example",
        http_delete=stub,
    )

    client.deregister("tts-a")

    assert stub.calls[0]["url"] == "https://playground.example/playground/workers/tts-a"


def test_deregister_treats_404_as_success() -> None:
    stub = _DeleteCall(_HttpResponse(status_code=404, text=""))
    client = PlaygroundRegistryClient(
        base_url="https://p.example",
        http_delete=stub,
    )
    client.deregister("w-1")


def test_deregister_raises_on_500() -> None:
    stub = _DeleteCall(_HttpResponse(status_code=500, text="x"))
    client = PlaygroundRegistryClient(
        base_url="https://p.example",
        http_delete=stub,
    )

    with pytest.raises(RegistryDeregisterError, match="status=500"):
        client.deregister("w-1")


def test_deregister_wraps_network_error() -> None:
    stub = _DeleteCall(RuntimeError("dns failure"))
    client = PlaygroundRegistryClient(
        base_url="https://p.example",
        http_delete=stub,
    )

    with pytest.raises(RegistryDeregisterError, match="network error"):
        client.deregister("w-1")


def test_deregister_trims_trailing_slash_on_base_url() -> None:
    stub = _DeleteCall(_HttpResponse(status_code=204, text=""))
    client = PlaygroundRegistryClient(
        base_url="https://p.example/",
        http_delete=stub,
    )
    client.deregister("w-1")
    assert stub.calls[0]["url"] == "https://p.example/playground/workers/w-1"
