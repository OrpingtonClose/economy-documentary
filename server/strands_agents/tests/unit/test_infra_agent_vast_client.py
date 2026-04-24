"""Unit tests for the Vast.ai destroy client."""

from __future__ import annotations

import pytest

from strands_agents.infra_agent.vast_client import (
    VAST_API_KEY_ENV,
    VastAiClient,
    VastAiDestroyError,
    _HttpResponse,
)


class _DeleteCall:
    """Records every http_delete invocation."""

    def __init__(self, response: _HttpResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def __call__(
        self, *, url: str, headers: dict[str, str], timeout: float
    ) -> _HttpResponse:
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_destroy_happy_path_sends_bearer_auth() -> None:
    stub = _DeleteCall(_HttpResponse(status_code=200, text="{}"))
    client = VastAiClient(api_key="test-key", http_delete=stub)

    client.destroy_instance(42)

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["url"] == "https://console.vast.ai/api/v0/instances/42/"
    assert call["headers"]["Authorization"] == "Bearer test-key"


def test_destroy_treats_404_as_success() -> None:
    stub = _DeleteCall(_HttpResponse(status_code=404, text="not found"))
    client = VastAiClient(api_key="k", http_delete=stub)

    client.destroy_instance("99")


def test_destroy_raises_on_500() -> None:
    stub = _DeleteCall(_HttpResponse(status_code=500, text="boom"))
    client = VastAiClient(api_key="k", http_delete=stub)

    with pytest.raises(VastAiDestroyError, match="status=500"):
        client.destroy_instance("1")


def test_destroy_raises_on_401() -> None:
    stub = _DeleteCall(_HttpResponse(status_code=401, text="nope"))
    client = VastAiClient(api_key="k", http_delete=stub)

    with pytest.raises(VastAiDestroyError, match="status=401"):
        client.destroy_instance("1")


def test_destroy_wraps_network_error() -> None:
    stub = _DeleteCall(RuntimeError("connection refused"))
    client = VastAiClient(api_key="k", http_delete=stub)

    with pytest.raises(VastAiDestroyError, match="network error"):
        client.destroy_instance("1")


def test_destroy_uses_env_key_when_api_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _DeleteCall(_HttpResponse(status_code=200, text="{}"))
    monkeypatch.setenv(VAST_API_KEY_ENV, "env-key")
    client = VastAiClient(api_key=None, http_delete=stub)

    client.destroy_instance("5")

    assert stub.calls[0]["headers"]["Authorization"] == "Bearer env-key"


def test_destroy_raises_when_no_key_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _DeleteCall(_HttpResponse(status_code=200, text="{}"))
    monkeypatch.delenv(VAST_API_KEY_ENV, raising=False)
    client = VastAiClient(api_key=None, http_delete=stub)

    with pytest.raises(VastAiDestroyError, match="api key not set"):
        client.destroy_instance("5")

    assert stub.calls == []


def test_destroy_trims_trailing_slash_on_base_url() -> None:
    stub = _DeleteCall(_HttpResponse(status_code=200, text=""))
    client = VastAiClient(
        api_key="k",
        base_url="https://console.vast.ai/api/v0/",
        http_delete=stub,
    )

    client.destroy_instance("7")

    assert stub.calls[0]["url"] == "https://console.vast.ai/api/v0/instances/7/"
