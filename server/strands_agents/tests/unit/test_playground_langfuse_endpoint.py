"""Tests for the ``GET /playground/config/langfuse`` endpoint (slice 2).

The endpoint must:

* Mirror :func:`strands_agents.playground.langfuse.frontend_config`.
* Never leak credentials (``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_PUBLIC_KEY``
  must not appear in any form in the response body).
* Return a stable shape so the frontend can rely on ``enabled`` and
  ``host`` keys being present regardless of configuration state.
"""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Reload so the fresh env vars land before the router binds to
    # ``langfuse.frontend_config``.
    from strands_agents.playground import langfuse as lf_module

    importlib.reload(lf_module)
    import playground as playground_module

    importlib.reload(playground_module)
    from playground import router as playground_router

    app = FastAPI()
    app.include_router(playground_router)
    return TestClient(app)


def test_disabled_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    client = _build_client(monkeypatch)

    response = client.get("/playground/config/langfuse")
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "host": None}


def test_enabled_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-abc")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-xyz")
    monkeypatch.setenv("LANGFUSE_HOST", "https://obs.example.com")
    client = _build_client(monkeypatch)

    response = client.get("/playground/config/langfuse")
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "host": "https://obs.example.com",
    }


def test_endpoint_never_leaks_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard invariant — secret material must not appear on the wire.

    A bug that serialises the full config into the response would
    pass most type checks, so guard it with an explicit substring
    check against the response body and the serialised JSON.
    """
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-NEVER_LEAK_ME")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-NEVER_LEAK_ME")
    monkeypatch.setenv("LANGFUSE_HOST", "https://obs.example.com")
    client = _build_client(monkeypatch)

    response = client.get("/playground/config/langfuse")
    body = response.text
    assert "NEVER_LEAK_ME" not in body
    assert "pk-lf" not in body
    assert "sk-lf" not in body
    # Double-check via the parsed JSON shape to pin the contract.
    parsed = json.loads(body)
    assert set(parsed.keys()) == {"enabled", "host"}
