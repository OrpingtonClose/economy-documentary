"""Regression: the very first SSE event on POST / must be RUN_STARTED.

CopilotKit's AG-UI client strictly enforces that the first event on a
streaming connection is ``RUN_STARTED``.  We used to emit a
``CustomEvent(name="run_started")`` as the first chunk (for URL
stamping), which made CopilotKit reject the stream with
``"First event must be 'RUN_STARTED'"`` -- that broke every user chat
submission end-to-end, so the pipeline never saw a brief and the OTIO
timeline stayed ``idle``.

This test asserts the fix: ``_run_started_event`` now emits a real
``RunStartedEvent`` carrying the ADK thread/run ids.
"""

from __future__ import annotations

import json


def _extract_data_line(sse_chunk: str) -> dict:
    """Return the parsed JSON payload from the ``data:`` line of an SSE chunk."""
    for line in sse_chunk.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise AssertionError(f"no data line in SSE chunk: {sse_chunk!r}")


def test_run_started_event_emits_run_started_type():
    """AG-UI protocol violation regression: first event must be RUN_STARTED."""
    from server import _run_started_event

    sse = _run_started_event(
        "run-abc123",
        thread_id="thread-xyz",
        adk_run_id="adk-run-1",
    )
    payload = _extract_data_line(sse)

    assert payload["type"] == "RUN_STARTED", (
        "first SSE event on POST / must be RUN_STARTED or CopilotKit rejects "
        "the stream with 'First event must be RUN_STARTED' -- see ARCH-UI-B"
    )
    assert payload["threadId"] == "thread-xyz"
    assert payload["runId"] == "adk-run-1"


def test_run_started_event_defaults_thread_and_run_to_registry_id():
    """Fallback defaults preserve the single-arg call convention."""
    from server import _run_started_event

    sse = _run_started_event("run-only")
    payload = _extract_data_line(sse)

    assert payload["type"] == "RUN_STARTED"
    assert payload["threadId"] == "run-only"
    assert payload["runId"] == "run-only"
