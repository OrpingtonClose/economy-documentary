"""Unit tests for :mod:`strands_agents.tools.task_pool`."""

from __future__ import annotations

import threading
import time

import pytest

from strands_agents.tools.task_pool import AsyncTaskPool, make_task_tools


@pytest.fixture
def pool() -> AsyncTaskPool:
    p = AsyncTaskPool(max_workers=2)
    yield p
    p.shutdown(wait_for_completion=True)


def test_launch_returns_pending_state_immediately(pool: AsyncTaskPool) -> None:
    gate = threading.Event()
    state = pool.launch(task_type="tts", identity="scene-1", fn=gate.wait)
    assert state.task_id
    assert state.task_type == "tts"
    assert state.identity == "scene-1"
    assert state.status in {"pending", "running"}
    gate.set()
    pool.await_all([state.task_id], timeout=5.0)


def test_launch_runs_fn_and_captures_result(pool: AsyncTaskPool) -> None:
    state = pool.launch(
        task_type="tts", identity="scene-1", fn=lambda: {"wav_path": "/tmp/a.wav"}
    )
    snapshots = pool.await_all([state.task_id], timeout=5.0)
    assert snapshots[0]["status"] == "complete"
    assert snapshots[0]["result"] == {"wav_path": "/tmp/a.wav"}
    assert snapshots[0]["error"] is None


def test_launch_is_idempotent_on_identity(pool: AsyncTaskPool) -> None:
    call_count = {"n": 0}

    def fn() -> dict[str, int]:
        call_count["n"] += 1
        return {"n": call_count["n"]}

    first = pool.launch(task_type="ltx", identity="scene-2", fn=fn)
    second = pool.launch(task_type="ltx", identity="scene-2", fn=fn)
    assert first.task_id == second.task_id
    pool.await_all([first.task_id], timeout=5.0)
    assert call_count["n"] == 1


def test_different_identities_create_different_tasks(pool: AsyncTaskPool) -> None:
    a = pool.launch(task_type="tts", identity="scene-1", fn=lambda: {"n": 1})
    b = pool.launch(task_type="tts", identity="scene-2", fn=lambda: {"n": 2})
    assert a.task_id != b.task_id


def test_failed_fn_marks_task_failed_with_error(pool: AsyncTaskPool) -> None:
    def boom() -> dict[str, str]:
        raise RuntimeError("worker exploded")

    state = pool.launch(task_type="ltx", identity="scene-3", fn=boom)
    snapshots = pool.await_all([state.task_id], timeout=5.0)
    assert snapshots[0]["status"] == "failed"
    assert "RuntimeError" in (snapshots[0]["error"] or "")
    assert "worker exploded" in (snapshots[0]["error"] or "")


def test_check_unknown_id_returns_not_found(pool: AsyncTaskPool) -> None:
    result = pool.check(["does-not-exist"])
    assert result == [{"task_id": "does-not-exist", "status": "not_found"}]


def test_await_all_respects_timeout(pool: AsyncTaskPool) -> None:
    gate = threading.Event()
    state = pool.launch(task_type="tts", identity="slow", fn=gate.wait)
    start = time.time()
    snapshots = pool.await_all([state.task_id], timeout=0.1)
    elapsed = time.time() - start
    assert elapsed < 1.0
    assert snapshots[0]["status"] in {"pending", "running"}
    gate.set()


def test_make_task_tools_exposes_check_and_await(pool: AsyncTaskPool) -> None:
    tools = make_task_tools(pool)
    assert set(tools) == {"check_tasks", "await_tasks"}
    state = pool.launch(task_type="tts", identity="scene-4", fn=lambda: {"ok": True})
    snapshots = tools["await_tasks"]([state.task_id], 5.0)
    assert snapshots[0]["status"] == "complete"
    # check_tasks is non-blocking.
    assert tools["check_tasks"]([state.task_id])[0]["status"] == "complete"


def test_shutdown_rejects_new_launches() -> None:
    p = AsyncTaskPool()
    p.shutdown(wait_for_completion=True)
    with pytest.raises(RuntimeError, match="shut down"):
        p.launch(task_type="tts", identity="x", fn=lambda: {})


def test_non_dict_result_is_wrapped(pool: AsyncTaskPool) -> None:
    state = pool.launch(task_type="noop", identity="ident", fn=lambda: "scalar")  # type: ignore[return-value]
    snapshots = pool.await_all([state.task_id], timeout=5.0)
    assert snapshots[0]["result"] == {"value": "scalar"}
