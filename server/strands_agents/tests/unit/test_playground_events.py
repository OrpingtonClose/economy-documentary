"""Unit tests for the event-driven run endpoints.

Covers:

* :class:`strands_agents.playground.events.RunStream` — emit, snapshot,
  ring-buffer bound, ``wait_for_after`` semantics (immediate return
  when new events exist; timeout returns empty list; closed stream
  returns empty list once everything has been seen).
* :class:`strands_agents.playground.events.RunRegistry` — LRU eviction.
* ``POST /components/{id}/runs`` + ``GET /runs/{run_id}`` end-to-end
  dispatch on a stubbed task adapter. Both terminal payload and
  event ordering are asserted.
* Reachability + task-error paths emit the expected terminal event
  kind (``run.error`` with the matching ``status`` in ``detail``).
* SSE replay — the stream includes every event emitted before the
  client subscribed.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from strands_agents.playground import (
    DeclaredModel,
    ReachabilityCache,
    ReachabilityStatus,
    get_component,
    set_default_cache,
)
from strands_agents.playground.events import Event, RunRegistry, RunStream


# --------------------------------------------------------------------------
# RunStream


@pytest.mark.asyncio
async def test_stream_emits_monotonic_sequence_numbers() -> None:
    stream = RunStream("run_abc", component_id="c02", case_name=None)
    e1 = await stream.emit("task.start", "start")
    e2 = await stream.emit("task.done", "done")
    assert e1.seq == 1
    assert e2.seq == 2
    assert e1.ts <= e2.ts
    assert [e.seq for e in stream.snapshot()] == [1, 2]


@pytest.mark.asyncio
async def test_stream_ring_buffer_bounds_memory() -> None:
    stream = RunStream(
        "run_ring", component_id="c02", case_name=None, max_events=5
    )
    for i in range(12):
        await stream.emit("tool.called", f"step {i}")
    snap = stream.snapshot()
    assert len(snap) == 5
    # Ring keeps the NEWEST events (seq 8..12), not the oldest.
    assert [e.seq for e in snap] == [8, 9, 10, 11, 12]


@pytest.mark.asyncio
async def test_wait_for_after_returns_immediately_when_new_events_exist() -> None:
    stream = RunStream("run_wait", component_id="c02", case_name=None)
    await stream.emit("probe.start", "start")
    await stream.emit("probe.done", "done")
    result = await stream.wait_for_after(last_seq=0, timeout=5.0)
    assert [e.seq for e in result] == [1, 2]


@pytest.mark.asyncio
async def test_wait_for_after_returns_empty_on_timeout() -> None:
    stream = RunStream("run_to", component_id="c02", case_name=None)
    await stream.emit("probe.start", "start")
    result = await stream.wait_for_after(last_seq=1, timeout=0.1)
    assert result == []


@pytest.mark.asyncio
async def test_wait_for_after_returns_empty_on_closed_stream() -> None:
    stream = RunStream("run_cl", component_id="c02", case_name=None)
    await stream.emit("probe.start", "start")
    await stream.close(terminal={"status": "OK"})
    result = await stream.wait_for_after(last_seq=1, timeout=5.0)
    assert result == []
    assert stream.closed
    assert stream.terminal == {"status": "OK"}


@pytest.mark.asyncio
async def test_wait_for_after_wakes_on_new_emit() -> None:
    stream = RunStream("run_wake", component_id="c02", case_name=None)

    async def emit_later() -> None:
        await asyncio.sleep(0.05)
        await stream.emit("tool.called", "late")

    waiter = asyncio.create_task(stream.wait_for_after(0, timeout=5.0))
    emitter = asyncio.create_task(emit_later())
    result = await waiter
    await emitter
    assert [e.kind for e in result] == ["tool.called"]


# --------------------------------------------------------------------------
# RunRegistry


def test_registry_evicts_oldest_runs_past_the_limit() -> None:
    registry = RunRegistry(max_runs=3)
    streams = [
        registry.new_run(component_id="c02", case_name=f"case_{i}")
        for i in range(5)
    ]
    assert registry.get(streams[0].run_id) is None
    assert registry.get(streams[1].run_id) is None
    assert registry.get(streams[4].run_id) is streams[4]
    assert len(registry.recent()) == 3


# --------------------------------------------------------------------------
# End-to-end: POST /runs → background dispatch → GET /runs/{id}


class _AlwaysReachableProber:
    def probe(self, model: DeclaredModel) -> ReachabilityStatus:
        return ReachabilityStatus(
            model_id=model.id,
            provider=model.provider,
            reachable=True,
            reason="ok",
            checked_at=0.0,
            latency_ms=0.0,
        )


class _AlwaysUnreachableProber:
    def probe(self, model: DeclaredModel) -> ReachabilityStatus:
        return ReachabilityStatus(
            model_id=model.id,
            provider=model.provider,
            reachable=False,
            reason="probe_error:AuthError",
            checked_at=0.0,
            latency_ms=0.0,
        )


def _make_client(disable_narrator: bool = True) -> TestClient:
    if disable_narrator:
        import os
        os.environ["PLAYGROUND_NARRATOR_DISABLE"] = "1"
    from playground import router as playground_router

    app = FastAPI()
    app.include_router(playground_router)
    return TestClient(app)


def _wait_for_terminal(
    client: TestClient, run_id: str, *, timeout: float = 10.0
) -> dict[str, Any]:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        res = client.get(f"/playground/runs/{run_id}").json()
        if res["closed"]:
            return res
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not terminate within {timeout}s")


def test_start_run_dispatches_and_records_ok_terminal() -> None:
    """Happy path: stubbed task adapter → OK terminal + ordered events."""
    component = get_component("c01")
    assert component is not None
    sentinel: dict[str, Any] = {"output": {"ok": True}, "trajectory": [1, 2]}
    previous_task = component._cache.get("task")
    component._cache["task"] = lambda _case: sentinel
    previous_cache = set_default_cache(
        ReachabilityCache(_AlwaysReachableProber())
    )
    try:
        client = _make_client()
        start = client.post(
            "/playground/components/c01/runs",
            json={"case_name": "economics_basics"},
        )
        assert start.status_code == 200
        body = start.json()
        run_id = body["run_id"]
        assert body["events_url"].endswith(f"/{run_id}/events")

        terminal = _wait_for_terminal(client, run_id)
        assert terminal["closed"] is True
        assert terminal["terminal"]["status"] == "OK"
        assert terminal["terminal"]["output"] == sentinel["output"]
        assert terminal["terminal"]["trajectory"] == sentinel["trajectory"]

        kinds = [e["kind"] for e in terminal["events"]]
        # Ordering invariant — probe → task → terminal.
        assert kinds[0] == "run.dispatched"
        assert "probe.start" in kinds
        assert "probe.done" in kinds
        assert "task.start" in kinds
        assert "task.done" in kinds
        assert kinds[-1] == "run.ok"
    finally:
        set_default_cache(previous_cache)
        if previous_task is None:
            component._cache.pop("task", None)
        else:
            component._cache["task"] = previous_task


def test_start_run_reports_model_unreachable_terminal() -> None:
    """Every declared model unreachable → run.error with MODEL_UNREACHABLE."""
    previous_cache = set_default_cache(
        ReachabilityCache(_AlwaysUnreachableProber())
    )
    try:
        client = _make_client()
        start = client.post(
            "/playground/components/c01/runs",
            json={"case_name": "economics_basics"},
        )
        assert start.status_code == 200
        run_id = start.json()["run_id"]

        terminal = _wait_for_terminal(client, run_id)
        assert terminal["terminal"]["status"] == "MODEL_UNREACHABLE"
        kinds = [e["kind"] for e in terminal["events"]]
        # No task.start on an unreachable gate — the dispatcher must
        # short-circuit BEFORE the adapter runs.
        assert "task.start" not in kinds
        assert kinds[-1] == "run.error"
        assert terminal["terminal"]["unreachable_models"], terminal["terminal"]
    finally:
        set_default_cache(previous_cache)


def test_start_run_reports_task_error_terminal() -> None:
    """Adapter raising → run.error terminal carries error_class."""
    component = get_component("c01")
    assert component is not None

    def _boom(_case: Any) -> Any:
        raise RuntimeError("synthetic failure from unit test")

    previous_task = component._cache.get("task")
    component._cache["task"] = _boom
    previous_cache = set_default_cache(
        ReachabilityCache(_AlwaysReachableProber())
    )
    try:
        client = _make_client()
        start = client.post(
            "/playground/components/c01/runs",
            json={"case_name": "economics_basics"},
        )
        assert start.status_code == 200
        run_id = start.json()["run_id"]

        terminal = _wait_for_terminal(client, run_id)
        assert terminal["terminal"]["status"] == "TASK_ERROR"
        assert terminal["terminal"]["error_class"] == "RuntimeError"
        assert "synthetic failure" in terminal["terminal"]["error"]
    finally:
        set_default_cache(previous_cache)
        if previous_task is None:
            component._cache.pop("task", None)
        else:
            component._cache["task"] = previous_task


def test_run_events_sse_replays_everything_emitted_before_subscribe() -> None:
    """SSE must surface events that landed before the client connected."""
    component = get_component("c01")
    assert component is not None
    previous_task = component._cache.get("task")
    component._cache["task"] = lambda _case: {"output": {"ok": True}}
    previous_cache = set_default_cache(
        ReachabilityCache(_AlwaysReachableProber())
    )
    try:
        client = _make_client()
        start = client.post(
            "/playground/components/c01/runs",
            json={"case_name": "economics_basics"},
        )
        run_id = start.json()["run_id"]
        _wait_for_terminal(client, run_id)

        # Subscribe AFTER the run finished — replay must still work.
        with client.stream(
            "GET", f"/playground/runs/{run_id}/events"
        ) as response:
            assert response.status_code == 200
            events: list[dict[str, Any]] = []
            for line in response.iter_lines():
                if not line or line.startswith(":"):
                    # Empty separator or heartbeat comment.
                    continue
                assert line.startswith("data: ")
                events.append(json.loads(line[len("data: "):]))
                if events[-1]["kind"] in {"run.ok", "run.error", "run.cancelled"}:
                    break

        kinds = [e["kind"] for e in events]
        assert "run.dispatched" in kinds
        assert kinds[-1] == "run.ok"
    finally:
        set_default_cache(previous_cache)
        if previous_task is None:
            component._cache.pop("task", None)
        else:
            component._cache["task"] = previous_task


def test_get_run_returns_404_for_unknown_run_id() -> None:
    client = _make_client()
    res = client.get("/playground/runs/run_does_not_exist")
    assert res.status_code == 404


def test_events_sse_returns_404_for_unknown_run_id() -> None:
    client = _make_client()
    res = client.get("/playground/runs/run_does_not_exist/events")
    assert res.status_code == 404


def test_list_recent_runs_lifts_dispatch_detail_and_filters_component_id() -> None:
    """``GET /playground/runs`` must return newest-first summaries with
    ``topic`` / ``target_duration_sec`` / ``language`` lifted from the
    ``run.dispatched`` event detail, and must respect ``component_id``
    filtering so the ``/pipeline`` sidebar isn't polluted with c01..c15
    component runs from the same registry.
    """
    component = get_component("c01")
    assert component is not None
    sentinel: dict[str, Any] = {"output": {"ok": True}}
    previous_task = component._cache.get("task")
    component._cache["task"] = lambda _case: sentinel
    previous_cache = set_default_cache(
        ReachabilityCache(_AlwaysReachableProber())
    )
    try:
        client = _make_client()

        #: Submit one component run (c01) and one pipeline run. The
        #: sidebar query filters to ``component_id=pipeline`` so only
        #: the pipeline run should come back.
        component_start = client.post(
            "/playground/components/c01/runs",
            json={"case_name": "economics_basics"},
        )
        assert component_start.status_code == 200
        _wait_for_terminal(client, component_start.json()["run_id"])

        #: Fabricate a pipeline run by hand-emitting against the
        #: registry — the real ``/playground/pipeline/runs`` requires
        #: live worker URLs which aren't available here. The test only
        #: cares that the endpoint reads ``component_id`` and lifts
        #: ``run.dispatched`` detail correctly.
        from playground import get_registry

        registry = get_registry()
        pipeline_stream = registry.new_run(
            component_id="pipeline", case_name=None
        )

        async def _seed_dispatched() -> None:
            await pipeline_stream.emit(
                "run.dispatched",
                "starting documentary pipeline",
                detail={
                    "topic": "The Federal Reserve",
                    "target_duration_sec": 90,
                    "language": "en",
                },
            )

        asyncio.run(_seed_dispatched())

        unfiltered = client.get("/playground/runs?limit=20").json()
        assert "runs" in unfiltered
        assert any(r["component_id"] == "c01" for r in unfiltered["runs"])
        assert any(r["component_id"] == "pipeline" for r in unfiltered["runs"])

        filtered = client.get(
            "/playground/runs?limit=20&component_id=pipeline"
        ).json()
        assert all(r["component_id"] == "pipeline" for r in filtered["runs"])
        pipeline_row = next(
            r
            for r in filtered["runs"]
            if r["run_id"] == pipeline_stream.run_id
        )
        assert pipeline_row["topic"] == "The Federal Reserve"
        assert pipeline_row["target_duration_sec"] == 90
        assert pipeline_row["language"] == "en"
        assert pipeline_row["closed"] is False
        assert pipeline_row["last_event_kind"] == "run.dispatched"
    finally:
        set_default_cache(previous_cache)
        if previous_task is None:
            component._cache.pop("task", None)
        else:
            component._cache["task"] = previous_task


# Unused Event import guard: keep the symbol in scope so future regression
# guards don't have to re-import when extending this module.
_ = Event
