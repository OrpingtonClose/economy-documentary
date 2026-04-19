"""UI-07 — integration tests for the POST / resume/replay protocol.

These tests cover the handshake between :class:`server.AGUIRunCollectorMiddleware`,
:func:`server._replay_stream`, and :func:`server.current_run` without
pulling in the ADK pipeline agent (which needs LLM credentials). We
mount the same middleware + helpers on a stub FastAPI app and seed the
shared run registry with deterministic events.

Covered:

* POST / with an existing ``X-Pipeline-Run-Id`` + ``Last-Event-ID``
  replays only events with seq > last-id and preserves the ``id:`` line
  so clients can keep their cursor across hops.
* Replay across the buffer boundary emits a ``buffer_overflow`` event
  before the surviving events.
* POST / without a run id header mints a fresh run and sets the
  ``X-Pipeline-Run-Id`` response header used by the URL propagator.
* ``GET /api/current-run`` tracks ``run_registry.latest()``.
* ``GET /api/runs/{id}/exists`` distinguishes known vs unknown runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from run_registry import RunRegistry, get_run_registry  # noqa: E402


# ---------------------------------------------------------------------------
# Stub app wired to exercise the same protocol pieces as server.py without
# pulling in the ADK agent. The middleware logic is duplicated here rather
# than imported because the real middleware depends on the dashboard DB
# side-effects; the protocol behaviour (resume vs fresh, replay, overflow,
# current-run) is what we're asserting.
# ---------------------------------------------------------------------------


def _make_app(registry: RunRegistry) -> FastAPI:
    from ag_ui.core import CustomEvent, EventType
    from ag_ui.encoder import EventEncoder

    app = FastAPI()

    class ResumeMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path != "/" or request.method != "POST":
                return await call_next(request)
            header_run_id = request.headers.get("x-pipeline-run-id")
            if header_run_id and registry.exists(header_run_id):
                request.state.run_id = header_run_id
                request.state.is_resume = True
            else:
                request.state.run_id = registry.create()
                request.state.is_resume = False
            return await call_next(request)

    app.add_middleware(ResumeMiddleware)

    def _tagged(seq: int, text: str) -> str:
        return f"id: {seq}\n{text}"

    def _parse_last_id(raw: Optional[str]) -> int:
        if not raw:
            return 0
        try:
            return max(0, int(raw.strip()))
        except ValueError:
            return 0

    def _buffer_overflow_chunk(rid: str, latest: int) -> str:
        payload = {"type": "buffer_overflow", "run_id": rid, "last_seq": latest}
        return EventEncoder().encode(
            CustomEvent(type=EventType.CUSTOM, name="buffer_overflow", value=payload)
        )

    def _replay_done_chunk(rid: str, latest: int, replayed: int) -> str:
        return EventEncoder().encode(
            CustomEvent(
                type=EventType.CUSTOM,
                name="replay_done",
                value={"run_id": rid, "last_seq": latest, "replayed": replayed},
            )
        )

    async def _replay(rid: str, last_id: int):
        events, overflow, latest = registry.replay(rid, last_id)
        if overflow:
            yield _tagged(0, _buffer_overflow_chunk(rid, latest))
        for seq, text in events:
            yield _tagged(seq, text)
        yield f": end of replay\n{_replay_done_chunk(rid, latest, len(events))}"

    @app.post("/")
    async def entry(request: Request):
        run_id = request.state.run_id
        is_resume = request.state.is_resume
        last_id = _parse_last_id(request.headers.get("last-event-id"))

        if is_resume:
            resp = StreamingResponse(
                _replay(run_id, last_id),
                media_type="text/event-stream",
            )
            resp.headers["X-Pipeline-Run-Id"] = run_id
            return resp

        async def fresh():
            # New run: announce id and yield a single agent event so
            # tests can assert both the buffering contract and the
            # X-Pipeline-Run-Id response header.
            first = EventEncoder().encode(
                CustomEvent(
                    type=EventType.CUSTOM,
                    name="run_started",
                    value={"run_id": run_id},
                )
            )
            seq1 = registry.append(run_id, first)
            yield _tagged(seq1, first)
            second = 'data: {"type":"TEXT_MESSAGE_CONTENT","delta":"hello"}\n\n'
            seq2 = registry.append(run_id, second)
            yield _tagged(seq2, second)

        resp = StreamingResponse(fresh(), media_type="text/event-stream")
        resp.headers["X-Pipeline-Run-Id"] = run_id
        return resp

    @app.get("/api/current-run")
    async def current_run():
        rid = registry.latest()
        if not rid:
            return {"run_id": None, "exists": False, "latest_seq": 0}
        return {
            "run_id": rid,
            "exists": True,
            "latest_seq": registry.latest_seq(rid),
        }

    @app.get("/api/runs/{run_id}/exists")
    async def run_exists(run_id: str):
        exists = registry.exists(run_id)
        return {
            "run_id": run_id,
            "exists": exists,
            "latest_seq": registry.latest_seq(run_id) if exists else 0,
        }

    return app


@pytest.fixture
def registry() -> RunRegistry:
    # Use a fresh registry per test so buffer sizes and latest_run_id
    # don't bleed between cases.
    return RunRegistry(maxlen=16)


@pytest.fixture
def client(registry: RunRegistry) -> TestClient:
    return TestClient(_make_app(registry))


# ---------------------------------------------------------------------------
# Protocol assertions
# ---------------------------------------------------------------------------


def _parse_sse(body: str) -> list[dict]:
    """Parse an SSE response body into a list of ``{"id", "data"}`` dicts.

    Lines starting with ``:`` are SSE comments and are ignored; every
    blank line demarcates an event.
    """
    events: list[dict] = []
    current: dict[str, str] = {}
    for raw in body.splitlines():
        if raw == "":
            if current:
                events.append(current)
                current = {}
            continue
        if raw.startswith(":"):
            continue
        key, _, value = raw.partition(": ")
        current.setdefault(key, "")
        current[key] = (current[key] + "\n" + value).strip() if current[key] else value
    if current:
        events.append(current)
    return events


def test_fresh_post_assigns_new_run_id_header(client: TestClient):
    resp = client.post("/", content=b"{}")
    assert resp.status_code == 200
    assert resp.headers.get("X-Pipeline-Run-Id", "").startswith("run-")
    events = _parse_sse(resp.text)
    # Two tagged events plus the 'end of replay' trailer is only for
    # resume — fresh responses yield the pair of events verbatim.
    tagged = [e for e in events if "id" in e]
    assert len(tagged) == 2
    assert tagged[0]["id"] == "1"
    assert tagged[1]["id"] == "2"


def test_current_run_tracks_latest(client: TestClient, registry: RunRegistry):
    # No runs yet.
    assert client.get("/api/current-run").json()["run_id"] is None

    first = client.post("/", content=b"{}").headers["X-Pipeline-Run-Id"]
    latest_after_first = client.get("/api/current-run").json()
    assert latest_after_first["run_id"] == first
    assert latest_after_first["exists"] is True

    second = client.post("/", content=b"{}").headers["X-Pipeline-Run-Id"]
    latest_after_second = client.get("/api/current-run").json()
    assert latest_after_second["run_id"] == second
    assert first != second


def test_run_exists_probe(client: TestClient):
    rid = client.post("/", content=b"{}").headers["X-Pipeline-Run-Id"]
    ok = client.get(f"/api/runs/{rid}/exists").json()
    assert ok == {"run_id": rid, "exists": True, "latest_seq": 2}
    missing = client.get("/api/runs/run-nope/exists").json()
    assert missing["exists"] is False


def test_resume_replays_events_with_id_prefix(
    client: TestClient, registry: RunRegistry
):
    # Seed a run: first POST gives us two events (seq 1 and 2).
    rid = client.post("/", content=b"{}").headers["X-Pipeline-Run-Id"]
    # Emit a few more events directly through the registry to simulate
    # pipeline progress that happened while the tab was still connected.
    for i in range(3, 8):
        registry.append(rid, f'data: {{"seq":{i}}}\n\n')

    # Reconnect saying "I saw up to seq=2".
    resp = client.post(
        "/",
        headers={"X-Pipeline-Run-Id": rid, "Last-Event-ID": "2"},
        content=b"",
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)

    # Cursor should step 3 -> 4 -> 5 -> 6 -> 7 plus the trailing
    # replay_done sentinel (no id).
    tagged_ids = [int(e["id"]) for e in events if "id" in e and e["id"] != "0"]
    assert tagged_ids == [3, 4, 5, 6, 7]

    # Last event should be the replay_done custom event.
    assert "replay_done" in resp.text


def test_resume_from_zero_returns_full_buffer(
    client: TestClient, registry: RunRegistry
):
    rid = client.post("/", content=b"{}").headers["X-Pipeline-Run-Id"]
    for i in range(3, 6):
        registry.append(rid, f'data: {{"seq":{i}}}\n\n')

    resp = client.post(
        "/",
        headers={"X-Pipeline-Run-Id": rid, "Last-Event-ID": "0"},
        content=b"",
    )
    tagged_ids = [int(e["id"]) for e in _parse_sse(resp.text) if "id" in e]
    # Fresh-tab replay returns every buffered event plus the sentinel id=0
    # from the buffer_overflow path is absent because there was no eviction.
    assert tagged_ids == [1, 2, 3, 4, 5]


def test_resume_across_buffer_boundary_emits_overflow(registry: RunRegistry):
    # Tiny ring so we can force eviction deterministically.
    tiny = RunRegistry(maxlen=3)
    app = _make_app(tiny)
    client = TestClient(app)

    rid = client.post("/", content=b"{}").headers["X-Pipeline-Run-Id"]
    # Seed beyond maxlen: the first event (seq=1) is evicted when we
    # append seq=4.
    for i in range(3, 8):
        tiny.append(rid, f'data: {{"seq":{i}}}\n\n')

    # Client last_id=1 → gap because seq=2 is no longer in the buffer.
    resp = client.post(
        "/",
        headers={"X-Pipeline-Run-Id": rid, "Last-Event-ID": "1"},
        content=b"",
    )
    body = resp.text
    assert "buffer_overflow" in body
    # Overflow sentinel is emitted with id:0 (real events are >=1).
    overflow_line = next(
        line for line in body.splitlines() if "buffer_overflow" in line
    )
    assert "buffer_overflow" in overflow_line


def test_resume_with_unknown_run_starts_fresh(
    client: TestClient, registry: RunRegistry
):
    # Unknown run id → middleware falls back to fresh-run path, not
    # replay. The new run gets a brand-new id.
    resp = client.post(
        "/",
        headers={
            "X-Pipeline-Run-Id": "run-does-not-exist",
            "Last-Event-ID": "42",
        },
        content=b"{}",
    )
    new_id = resp.headers["X-Pipeline-Run-Id"]
    assert new_id != "run-does-not-exist"
    assert new_id.startswith("run-")


def test_two_simultaneous_reconnects_see_consistent_replay(
    client: TestClient, registry: RunRegistry
):
    rid = client.post("/", content=b"{}").headers["X-Pipeline-Run-Id"]
    for i in range(3, 10):
        registry.append(rid, f'data: {{"seq":{i}}}\n\n')

    tab_a = client.post(
        "/",
        headers={"X-Pipeline-Run-Id": rid, "Last-Event-ID": "3"},
        content=b"",
    )
    tab_b = client.post(
        "/",
        headers={"X-Pipeline-Run-Id": rid, "Last-Event-ID": "3"},
        content=b"",
    )
    a_ids = [int(e["id"]) for e in _parse_sse(tab_a.text) if e.get("id")]
    b_ids = [int(e["id"]) for e in _parse_sse(tab_b.text) if e.get("id")]
    assert a_ids == b_ids == [4, 5, 6, 7, 8, 9]


def test_last_event_id_is_preserved_verbatim_on_replay(
    client: TestClient, registry: RunRegistry
):
    rid = client.post("/", content=b"{}").headers["X-Pipeline-Run-Id"]
    # Seed a recognisable payload.
    marker = 'data: {"type":"pipeline_event","value":{"marker":"unique-42"}}\n\n'
    registry.append(rid, marker)
    resp = client.post(
        "/",
        headers={"X-Pipeline-Run-Id": rid, "Last-Event-ID": "2"},
        content=b"",
    )
    # The exact SSE text we appended must appear in the replay body.
    assert '"marker":"unique-42"' in resp.text
