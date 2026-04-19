"""UI-PIPE (#235): test that the AG-UI endpoint stages the user's brief
into the ADK session state before invoking the pipeline.

Regression guard for the journey-B blocker: without brief propagation,
scenario_director aborts with "no brief_text provided" and the agent run
ends immediately, so narrator events emitted afterward are rejected by
CopilotKit's ``RUN_FINISHED`` guard and never reach the chat stream.

This test exercises just the message-extraction + state-injection logic
by directly invoking the endpoint with a mocked ``adk_agent.run`` so it
exits after observing the staged state.  It does not boot the real
pipeline.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _build_input_payload(user_text: str, initial_state: dict | None = None) -> dict:
    """Mirror a minimal AG-UI RunAgentInput body."""
    return {
        "thread_id": "t-1",
        "run_id": "r-1",
        "state": initial_state or {},
        "messages": [
            {
                "id": "m-1",
                "role": "user",
                "content": user_text,
            }
        ],
        "tools": [],
        "context": [],
        "forwarded_props": {},
    }


@pytest.fixture
def server_module(monkeypatch):
    """Import server with env hooks disabled to avoid heavy startup."""
    monkeypatch.setenv("ADK_MODEL", "gemini-2.5-flash-lite")
    monkeypatch.delenv("DOCUMENTARY_AUDIT_HOOKS", raising=False)
    # Import lazily so env changes apply.
    import importlib
    import server as server_mod  # noqa: WPS433 (import-side-effects acceptable in test)

    return importlib.reload(server_mod)


def test_user_brief_propagates_into_state(server_module):
    """A user chat message is staged as state['topic'] + state[ORIGINAL_BRIEF_KEY]
    before the ADK agent sees the input."""

    observed: dict[str, Any] = {"state": None}

    async def _fake_run(input_data):
        # Snapshot the state as the agent would receive it.
        observed["state"] = dict(input_data.state or {})
        # Return an empty async iterator so the SSE stream ends.
        if False:
            yield None  # pragma: no cover

    class _FakeAgent:
        def run(self, input_data):
            return _fake_run(input_data)

    with patch.object(server_module, "adk_agent", _FakeAgent()):
        client = TestClient(server_module.app)
        resp = client.post(
            "/",
            json=_build_input_payload(
                "Make a 3-minute documentary about otters building dams.",
            ),
            headers={"Accept": "text/event-stream"},
        )
        # Drain the stream so the endpoint runs the propagation block.
        _ = resp.content

    state = observed["state"] or {}
    assert state.get("topic") == "Make a 3-minute documentary about otters building dams."
    from callbacks.run_start_seed import ORIGINAL_BRIEF_KEY

    assert state.get(ORIGINAL_BRIEF_KEY) == state.get("topic")


def test_user_brief_does_not_overwrite_existing_topic(server_module):
    """If the client already staged state['topic'] we respect it and don't overwrite."""

    observed: dict[str, Any] = {"state": None}

    async def _fake_run(input_data):
        observed["state"] = dict(input_data.state or {})
        if False:
            yield None  # pragma: no cover

    class _FakeAgent:
        def run(self, input_data):
            return _fake_run(input_data)

    with patch.object(server_module, "adk_agent", _FakeAgent()):
        client = TestClient(server_module.app)
        client.post(
            "/",
            json=_build_input_payload(
                "Otters, please.",
                initial_state={"topic": "beavers"},
            ),
            headers={"Accept": "text/event-stream"},
        )

    state = observed["state"] or {}
    assert state.get("topic") == "beavers"  # existing state wins


def test_null_topic_is_overwritten(server_module):
    """Regression guard: frontend sometimes sends state={'topic': null}.
    str(None) is 'None' (truthy), so a naive guard silently skips
    propagation and the pipeline aborts.  The ``or ""`` fix must treat
    a ``None`` value as empty and stage the user brief normally."""

    observed: dict[str, Any] = {"state": None}

    async def _fake_run(input_data):
        observed["state"] = dict(input_data.state or {})
        if False:
            yield None  # pragma: no cover

    class _FakeAgent:
        def run(self, input_data):
            return _fake_run(input_data)

    from callbacks.run_start_seed import ORIGINAL_BRIEF_KEY

    with patch.object(server_module, "adk_agent", _FakeAgent()):
        client = TestClient(server_module.app)
        client.post(
            "/",
            json=_build_input_payload(
                "A doc about otters.",
                initial_state={"topic": None, ORIGINAL_BRIEF_KEY: None},
            ),
            headers={"Accept": "text/event-stream"},
        )

    state = observed["state"] or {}
    assert state.get("topic") == "A doc about otters."
    assert state.get(ORIGINAL_BRIEF_KEY) == "A doc about otters."


def test_multimodal_content_parts_extracted(server_module):
    """User messages with a list of content parts are joined by text."""

    observed: dict[str, Any] = {"state": None}

    async def _fake_run(input_data):
        observed["state"] = dict(input_data.state or {})
        if False:
            yield None  # pragma: no cover

    class _FakeAgent:
        def run(self, input_data):
            return _fake_run(input_data)

    payload = {
        "thread_id": "t-2",
        "run_id": "r-2",
        "state": {},
        "messages": [
            {
                "id": "m-2",
                "role": "user",
                "content": [
                    {"type": "text", "text": "Make a film about"},
                    {"type": "text", "text": "honeybees"},
                ],
            }
        ],
        "tools": [],
        "context": [],
        "forwarded_props": {},
    }

    with patch.object(server_module, "adk_agent", _FakeAgent()):
        client = TestClient(server_module.app)
        client.post("/", json=payload, headers={"Accept": "text/event-stream"})

    assert "honeybees" in (observed["state"] or {}).get("topic", "")
