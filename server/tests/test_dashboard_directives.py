"""Tests for the ARCH-H4 dashboard directive + halt endpoints (#159).

Covers the behavioural contract declared in
``server/dashboard_directives.py``:

1. ``POST /api/halt`` engages the disk-backed halt flag.
2. ``POST /api/halt/release`` clears it.
3. ``GET /api/halt_state`` mirrors the on-disk state.
4. ``POST /api/directive`` parses through A2, appends to A1, and
   triggers A5 consistency checking.
5. ``slot_context`` on the directive payload becomes the A2 scope hint
   and the resulting record is scoped accordingly.
6. A2 parse failure surfaces as 422 with the interpreter message.
7. The approval-gate poll loop observes the halt flag and blocks even
   when the stage is otherwise approved.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Make ``server/`` imports resolve when pytest is launched from the repo
# root (mirrors test_preference_interpreter.py).
_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from agents import preference_interpreter as pi  # noqa: E402
from agents.preference_interpreter import (  # noqa: E402
    set_llm_client_factory,
)
from callbacks import approval_gate  # noqa: E402
from callbacks.preference_ledger import (  # noqa: E402
    PREFERENCE_LEDGER_KEY,
    Polarity,
    Scope,
    Subject,
    list_preferences,
)
import dashboard_directives as dd  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    """Point every disk-backed state file at a throwaway directory.

    Also flips the approval-gate poll interval to a millisecond so the
    halt-blocking test can observe the block without sleeping for 5
    seconds per iteration.
    """
    monkeypatch.setenv("PIPELINE_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(dd, "_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        dd, "_HALT_FILE", os.path.join(str(tmp_path), ".halt_state.json")
    )
    monkeypatch.setattr(
        dd,
        "_BLACKBOARD_FILE",
        os.path.join(str(tmp_path), ".dashboard_blackboard.json"),
    )
    monkeypatch.setattr(
        approval_gate,
        "_APPROVAL_FILE",
        os.path.join(str(tmp_path), ".approval_state.json"),
    )
    # Disable auto-approval so wait_for_approval actually polls.
    monkeypatch.setattr(approval_gate, "_AUTO_APPROVE_ENV", False)
    monkeypatch.setattr(
        approval_gate, "_should_auto_approve", lambda: False
    )
    # Tight poll interval so the halt-blocking test runs in <1s.
    monkeypatch.setattr(approval_gate, "_POLL_INTERVAL", 0.02)
    yield tmp_path


@pytest.fixture
def client(output_dir):
    """Build a minimal FastAPI app exposing the dashboard_directives router."""
    app = FastAPI()
    app.include_router(dd.router)
    return TestClient(app)


def _stub_llm_records(records: list[dict[str, Any]]) -> Callable[[], pi.LLMCallable]:
    payload = json.dumps({"records": records})

    def factory() -> pi.LLMCallable:
        def call(model: str, system: str, prompt: str) -> str:
            return payload

        return call

    return factory


@pytest.fixture(autouse=True)
def _reset_llm_factory():
    yield
    set_llm_client_factory(None)


# ---------------------------------------------------------------------------
# /api/halt + /api/halt_state + /api/halt/release
# ---------------------------------------------------------------------------


def test_halt_flag_round_trip(client):
    # initial state is not halted
    r = client.get("/api/halt_state")
    assert r.status_code == 200
    data = r.json()
    assert data["halt_requested"] is False
    assert data["halted_at_stage"] is None

    # POST /api/halt engages the flag
    r = client.post("/api/halt", json={"reviewer": "alice", "reason": "jarring"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "halt_requested"
    assert body["halt_requested"] is True
    assert body["halt_reviewer"] == "alice"
    assert body["halt_reason"] == "jarring"

    # GET /api/halt_state mirrors disk
    r = client.get("/api/halt_state")
    assert r.status_code == 200
    assert r.json()["halt_requested"] is True
    assert dd.is_halt_requested() is True

    # POST /api/halt/release clears
    r = client.post("/api/halt/release")
    assert r.status_code == 200
    assert r.json()["halt_requested"] is False

    r = client.get("/api/halt_state")
    assert r.json()["halt_requested"] is False
    assert dd.is_halt_requested() is False


def test_halt_accepts_empty_body(client):
    """The halt button fires without a body; endpoint must not 400."""
    r = client.post("/api/halt")
    assert r.status_code == 200
    assert r.json()["halt_requested"] is True


def test_halt_state_survives_restart(client, output_dir):
    """Halt state is disk-backed so a separate process observes the flag."""
    client.post("/api/halt", json={"reviewer": "bob"})
    halt_path = os.path.join(str(output_dir), ".halt_state.json")
    assert os.path.exists(halt_path)
    with open(halt_path) as f:
        data = json.load(f)
    assert data["halt_requested"] is True
    assert data["halt_reviewer"] == "bob"


# ---------------------------------------------------------------------------
# /api/directive -- happy path
# ---------------------------------------------------------------------------


def test_directive_global(client):
    set_llm_client_factory(
        _stub_llm_records(
            [
                {
                    "scope": "global",
                    "scope_ref": None,
                    "polarity": "prefer",
                    "subject": "duration",
                    "content": "shorter narration",
                }
            ]
        )
    )

    r = client.post(
        "/api/directive",
        json={
            "directive": "I prefer shorter narration",
            "reviewer": "alice",
            "l4_event_id": "L4-001",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "accepted"
    assert data["l4_event_id"] == "L4-001"
    assert len(data["record_ids"]) == 1
    assert data["record_ids"][0] == 1
    record = data["records"][0]
    assert record["scope"] == Scope.GLOBAL.value
    assert record["polarity"] == Polarity.PREFER.value
    assert record["subject"] == Subject.DURATION.value
    assert data["scope_hint"] is None
    # Second call increments revision -- ledger is append-only.
    r2 = client.post(
        "/api/directive",
        json={
            "directive": "no more narrator voice",
            "reviewer": "alice",
            "l4_event_id": "L4-002",
        },
    )
    assert r2.status_code == 200
    assert r2.json()["record_ids"][0] == 2


def test_directive_scope_hint_from_slot_context(client):
    """slot_context with scene_num gets translated into a scene scope hint
    and the resulting record is scoped to that scene -- even when the
    directive itself carries no scene reference."""
    # Force the heuristic path so we don't depend on LLM behaviour for
    # scope inference -- _apply_scope_hint is belt-and-braces for the LLM
    # case and the primary path for heuristics.
    def bad_factory() -> pi.LLMCallable:
        def call(model: str, system: str, prompt: str) -> str:
            return ""

        return call

    set_llm_client_factory(bad_factory)

    r = client.post(
        "/api/directive",
        json={
            "directive": "make it louder",
            "reviewer": "alice",
            "l4_event_id": "L4-010",
            "slot_context": {"scene_num": 3},
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["scope_hint"] == {"scope": "scene", "scope_ref": "scene-3"}
    assert len(data["records"]) >= 1
    rec = data["records"][0]
    assert rec["scope"] == Scope.SCENE.value
    assert rec["scope_ref"] == "scene-3"


def test_directive_explicit_scope_overrides_slot_inference(client):
    """Callers can bypass the slot-key heuristic by supplying scope / scope_ref
    directly on slot_context."""
    def bad_factory() -> pi.LLMCallable:
        def call(model: str, system: str, prompt: str) -> str:
            return ""

        return call

    set_llm_client_factory(bad_factory)

    r = client.post(
        "/api/directive",
        json={
            "directive": "please make it warmer",
            "reviewer": "bob",
            "l4_event_id": "L4-011",
            "slot_context": {
                "scope": "voice_block",
                "scope_ref": "Cassandra",
            },
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["scope_hint"] == {
        "scope": "voice_block",
        "scope_ref": "Cassandra",
    }
    rec = data["records"][0]
    assert rec["scope"] == Scope.VOICE_BLOCK.value
    assert rec["scope_ref"] == "Cassandra"


def test_directive_generalisation_beats_scope_hint(client):
    """If the directive explicitly generalises, the scope hint is not applied."""
    def bad_factory() -> pi.LLMCallable:
        def call(model: str, system: str, prompt: str) -> str:
            return ""

        return call

    set_llm_client_factory(bad_factory)

    r = client.post(
        "/api/directive",
        json={
            "directive": "globally prefer shorter narration",
            "reviewer": "alice",
            "l4_event_id": "L4-012",
            "slot_context": {"scene_num": 5},
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # Hint was translated but A2 refused to apply it because the
    # directive generalises.
    assert data["scope_hint"] == {"scope": "scene", "scope_ref": "scene-5"}
    scopes = {r["scope"] for r in data["records"]}
    assert Scope.GLOBAL.value in scopes


# ---------------------------------------------------------------------------
# /api/directive -- error paths
# ---------------------------------------------------------------------------


def test_directive_empty_body_returns_400(client):
    r = client.post("/api/directive", json={})
    assert r.status_code == 400
    assert "directive" in r.json()["error"]


def test_directive_blank_string_returns_400(client):
    r = client.post("/api/directive", json={"directive": "   "})
    assert r.status_code == 400


def test_directive_bad_slot_context_returns_400(client):
    r = client.post(
        "/api/directive",
        json={"directive": "anything", "slot_context": "not-an-object"},
    )
    assert r.status_code == 400


def test_directive_interpreter_failure_returns_422(client, monkeypatch):
    """A2 parse failure -> 422 with the InterpreterError message."""
    def raise_interpreter_error(*args, **kwargs):
        raise pi.InterpreterError("closed-vocab miss: scope='bogus'")

    monkeypatch.setattr(dd, "interpret_directive", raise_interpreter_error)

    r = client.post(
        "/api/directive",
        json={"directive": "anything", "reviewer": "alice"},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["kind"] == "interpreter_parse_failure"
    assert "closed-vocab" in body["error"]


def test_directive_consistency_failure_returns_500(client, monkeypatch):
    """A wiring bug in A5 surfaces as a fail-loud 500."""
    set_llm_client_factory(
        _stub_llm_records(
            [
                {
                    "scope": "global",
                    "scope_ref": None,
                    "polarity": "prefer",
                    "subject": "tone",
                    "content": "warmer",
                }
            ]
        )
    )

    def raise_boom(*args, **kwargs):
        raise RuntimeError("synthetic A5 wiring bug")

    monkeypatch.setattr(dd, "check_consistency_at_gate", raise_boom)

    r = client.post(
        "/api/directive",
        json={"directive": "warmer overall", "reviewer": "alice"},
    )
    assert r.status_code == 500
    assert r.json()["kind"] == "consistency_check_failure"


# ---------------------------------------------------------------------------
# Scope-hint translator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slot_context,expected",
    [
        (None, None),
        ({}, None),
        ({"scene_id": "scene-7"}, {"scope": "scene", "scope_ref": "scene-7"}),
        ({"scene_num": 12}, {"scope": "scene", "scope_ref": "scene-12"}),
        ({"scene_num": "9"}, {"scope": "scene", "scope_ref": "scene-9"}),
        (
            {"voice_block_id": "vb-3"},
            {"scope": "voice_block", "scope_ref": "vb-3"},
        ),
        (
            {"clip_id": "clip-42"},
            {"scope": "element", "scope_ref": "clip-42"},
        ),
        (
            {"stage": "scenario"},
            {"scope": "stage", "scope_ref": "scenario"},
        ),
        (
            {"scope": "scene", "scope_ref": "scene-9"},
            {"scope": "scene", "scope_ref": "scene-9"},
        ),
    ],
)
def test_slot_context_to_scope_hint(slot_context, expected):
    assert dd._slot_context_to_scope_hint(slot_context) == expected


def test_slot_context_rejects_non_mapping():
    with pytest.raises(ValueError):
        dd._slot_context_to_scope_hint("scene-3")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Halt-at-next-boundary behaviour
# ---------------------------------------------------------------------------


def test_wait_for_approval_blocks_on_halt_flag(output_dir, monkeypatch):
    """When halt is engaged, wait_for_approval must NOT return even when
    the stage is otherwise approved -- it keeps polling until halt clears.
    """
    # Max wait tight enough that an undetected bug gives up quickly.
    monkeypatch.setattr(approval_gate, "_MAX_WAIT", 0.5)

    stage = "scenario"
    # Mark the stage approved FIRST -- this is the crux: without halt,
    # wait_for_approval would return immediately.
    approval_gate.approve_stage(stage)
    assert approval_gate.is_stage_approved(stage) is True

    # Engage halt.  wait_for_approval should observe it and keep blocking.
    dd.set_halt_requested(reviewer="tester")

    start = time.monotonic()
    approved = approval_gate.wait_for_approval(stage)
    elapsed = time.monotonic() - start

    # Halt held until timeout -> wait_for_approval returns False (timeout)
    # and elapsed is approximately _MAX_WAIT.
    assert approved is False
    assert elapsed >= 0.3, (
        f"wait_for_approval returned too quickly ({elapsed:.3f}s) -- "
        f"halt flag was not observed"
    )
    # halted_at_stage was recorded on disk the first time the loop saw
    # the flag.
    halt_state = dd._read_halt_state()
    assert halt_state["halted_at_stage"] == stage


def test_wait_for_approval_resumes_after_halt_cleared(output_dir, monkeypatch):
    """Clearing halt mid-poll lets the next tick see the approval."""
    monkeypatch.setattr(approval_gate, "_MAX_WAIT", 2.0)
    monkeypatch.setattr(approval_gate, "_POLL_INTERVAL", 0.02)

    stage = "scenario"
    approval_gate.approve_stage(stage)
    dd.set_halt_requested(reviewer="tester")

    # Clear halt from a background thread after a short delay.
    import threading

    def _clear_later():
        time.sleep(0.15)
        dd.clear_halt()

    t = threading.Thread(target=_clear_later, daemon=True)
    t.start()

    start = time.monotonic()
    approved = approval_gate.wait_for_approval(stage)
    elapsed = time.monotonic() - start
    t.join(timeout=1.0)

    assert approved is True
    assert elapsed >= 0.1
    assert elapsed < 1.5


def test_mark_halted_at_stage_cannot_clobber_clear_halt(output_dir):
    """Regression for the race pointed out on PR #181.

    :func:`clear_halt` (server process, triggered by ``/api/halt/release``)
    and :func:`mark_halted_at_stage` (pipeline process, called from the
    approval-gate poll loop) share the halt-state file.  A naive
    read-modify-write in ``mark_halted_at_stage`` could pick up a stale
    ``halt_requested: True`` and clobber a ``clear_halt`` that landed in
    between the read and the write -- silently re-engaging the halt the
    reviewer just released.  The :func:`_file_lock` guard must serialise
    the two cycles so this never happens.
    """
    import threading

    dd.set_halt_requested(reviewer="tester")
    assert dd.is_halt_requested() is True

    errors: list[BaseException] = []

    def _mark_many() -> None:
        try:
            for _ in range(200):
                dd.mark_halted_at_stage("scenario")
        except BaseException as exc:  # pragma: no cover -- defensive
            errors.append(exc)

    def _clear_once() -> None:
        try:
            # Let the marker loop get going, then release.
            time.sleep(0.01)
            dd.clear_halt()
        except BaseException as exc:  # pragma: no cover -- defensive
            errors.append(exc)

    marker = threading.Thread(target=_mark_many)
    clearer = threading.Thread(target=_clear_once)
    marker.start()
    clearer.start()
    marker.join(timeout=5.0)
    clearer.join(timeout=5.0)

    assert not errors, errors
    # After both threads finish, the halt flag MUST be cleared --
    # mark_halted_at_stage cannot resurrect it.
    assert dd.is_halt_requested() is False
    state = dd._read_halt_state()
    assert state["halt_requested"] is False


# ---------------------------------------------------------------------------
# Blackboard persistence
# ---------------------------------------------------------------------------


def test_directive_persists_ledger_on_disk(client, output_dir):
    set_llm_client_factory(
        _stub_llm_records(
            [
                {
                    "scope": "global",
                    "scope_ref": None,
                    "polarity": "prefer",
                    "subject": "pacing",
                    "content": "tighter pacing",
                }
            ]
        )
    )
    client.post(
        "/api/directive",
        json={"directive": "tighter pacing please", "reviewer": "alice"},
    )
    bb_path = os.path.join(str(output_dir), ".dashboard_blackboard.json")
    assert os.path.exists(bb_path)
    with open(bb_path) as f:
        data = json.load(f)
    assert PREFERENCE_LEDGER_KEY in data
    # Ledger round-trips through list_preferences cleanly.
    records = list_preferences(data)
    assert len(records) == 1
    assert records[0].subject is Subject.PACING
