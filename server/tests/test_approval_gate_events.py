"""Tests for UI-03a / UI-03c: inline approval-gate events and stage-
scoped directive -> gate release (issues #188, #198, #199, #200).

Covers:

1. ``wait_for_approval`` emits exactly **one** ``approval_gate_opened``
   event per entry and **one** ``approval_gate_closed`` event per exit.
   The open event MUST NOT fire per poll tick (DoD, #198).
2. Both events land on the shared AG-UI event bus, which is what
   the unified SSE endpoint at ``POST /`` *and* the ``/agui/stream``
   endpoint relay verbatim -- so the same events drive both the
   narrator (UI-01) and the timeline card (UI-03b).
3. A stage-scoped directive on ``POST /api/directive`` (``slot_context
   = {"stage": "scenario"}``) appends a ledger record AND releases the
   matching approval gate (#200). ``wait_for_approval`` then exits and
   emits ``approval_gate_closed`` with ``decision="approved"``.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import agui  # noqa: E402
from agents import preference_interpreter as pi  # noqa: E402
from agents.preference_interpreter import (  # noqa: E402
    set_llm_client_factory,
)
from callbacks import approval_gate  # noqa: E402
import dashboard_directives as dd  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_dashboard_directives.output_dir)
# ---------------------------------------------------------------------------


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
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
    monkeypatch.setattr(approval_gate, "_POLL_INTERVAL", 0.02)
    yield tmp_path


@pytest.fixture
def client(output_dir):
    app = FastAPI()
    app.include_router(dd.router)
    return TestClient(app)


def _stub_llm_records(
    records: list[dict[str, Any]],
) -> Callable[[], pi.LLMCallable]:
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
# UI-03a (#198): approval_gate_opened / _closed event emission
# ---------------------------------------------------------------------------


def _drain(queue) -> list[dict]:
    out: list[dict] = []
    while queue:
        out.append(queue.popleft())
    return out


def test_wait_for_approval_emits_open_and_close_once(output_dir, monkeypatch):
    """UI-03a #198 DoD:

    * ``approval_gate_opened`` fires ONCE per ``wait_for_approval`` entry,
      NOT on every poll tick.
    * ``approval_gate_closed`` fires ONCE on exit with the decision tag
      the narrator surfaces in chat.
    """
    # Keep the window short so the test runs fast, and the poll interval
    # tight so we get many ticks in that window -- if the gate-open event
    # were emitted per tick we would see many of them.
    monkeypatch.setattr(approval_gate, "_MAX_WAIT", 0.5)
    monkeypatch.setattr(approval_gate, "_POLL_INTERVAL", 0.01)

    stage = "scenario"
    queue = agui.subscribe_agui_events()
    try:
        # Flip the approval flag from a background thread after a few
        # poll ticks -- matches what /agui/approve does in production.
        def _approve_later():
            time.sleep(0.06)
            approval_gate.approve_stage(stage)

        threading.Thread(target=_approve_later, daemon=True).start()

        approved = approval_gate.wait_for_approval(stage)
        events = _drain(queue)
    finally:
        agui.unsubscribe_agui_events(queue)

    assert approved is True

    opens = [e for e in events if e["type"] == "approval_gate_opened"]
    closes = [e for e in events if e["type"] == "approval_gate_closed"]

    assert len(opens) == 1, (
        f"approval_gate_opened must fire exactly once per entry, "
        f"got {len(opens)} events: {opens}"
    )
    assert len(closes) == 1, (
        f"approval_gate_closed must fire exactly once per exit, "
        f"got {len(closes)} events: {closes}"
    )

    assert opens[0]["data"]["stage"] == stage
    assert "opened_at" in opens[0]["data"]
    assert closes[0]["data"]["stage"] == stage
    assert closes[0]["data"]["decision"] == "approved"


def test_wait_for_approval_emits_close_on_timeout(output_dir, monkeypatch):
    """Even when the gate times out, the paired close event MUST fire so
    the inline card and narrator do not stay stuck open forever."""
    monkeypatch.setattr(approval_gate, "_MAX_WAIT", 0.1)
    monkeypatch.setattr(approval_gate, "_POLL_INTERVAL", 0.01)

    stage = "prompts"
    queue = agui.subscribe_agui_events()
    try:
        approved = approval_gate.wait_for_approval(stage)
        events = _drain(queue)
    finally:
        agui.unsubscribe_agui_events(queue)

    assert approved is False
    opens = [e for e in events if e["type"] == "approval_gate_opened"]
    closes = [e for e in events if e["type"] == "approval_gate_closed"]
    assert len(opens) == 1
    assert len(closes) == 1
    assert closes[0]["data"]["decision"] == "timeout"


def test_approve_endpoint_accepts_every_gated_pipeline_stage(output_dir):
    """Regression: the inline approval card POSTs ``/agui/approve``
    with ``{stage: gate.stage}`` for whichever gate is currently open.
    ``pipeline.py`` opens gates for ``scenario``, ``audio``, ``prompts``,
    and ``clips``; all four MUST be accepted by the endpoint or the
    Approve button would return 400 for ``audio``.
    """
    # Late import to avoid circulars at test-collection time and so the
    # output_dir monkeypatches are in effect.
    import agui as agui_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(agui_module.router)
    client = TestClient(app)

    for stage in ("scenario", "audio", "prompts", "clips"):
        r = client.post("/agui/approve", json={"stage": stage})
        assert r.status_code == 200, (stage, r.text)
        assert r.json() == {"status": "approved", "stage": stage}
        assert approval_gate.is_stage_approved(stage) is True


def test_approve_stage_marks_flag_and_lets_gate_close(output_dir):
    """``/agui/approve`` flips the approval flag; this test mirrors that
    surface and checks the gate loop observes it and fires close."""
    # Drive the full end-to-end loop via wait_for_approval; that path
    # ensures the event emission contract holds regardless of which
    # endpoint flipped the flag.
    queue = agui.subscribe_agui_events()
    try:
        def _approve_later():
            time.sleep(0.05)
            approval_gate.approve_stage("audio")

        threading.Thread(target=_approve_later, daemon=True).start()

        approved = approval_gate.wait_for_approval("audio")
        events = _drain(queue)
    finally:
        agui.unsubscribe_agui_events(queue)

    assert approved is True
    closes = [e for e in events if e["type"] == "approval_gate_closed"]
    assert len(closes) == 1
    assert closes[0]["data"]["stage"] == "audio"
    assert closes[0]["data"]["decision"] == "approved"


# ---------------------------------------------------------------------------
# UI-03c (#200): stage-scoped directive = "reject with note"
# ---------------------------------------------------------------------------


def test_stage_scoped_directive_releases_gate(client, output_dir):
    """Clicking "Reject with note" on the inline approval card posts a
    ``/api/directive`` with ``slot_context = {"stage": <stage>}``.  The
    backend must:

      (a) interpret + append the directive to the Preference Ledger
          (existing contract; covered by test_dashboard_directives),
      (b) release the matching approval gate so the pipeline moves on
          (UI-03c, #200).
    """
    set_llm_client_factory(
        _stub_llm_records(
            [
                {
                    "scope": "stage",
                    "scope_ref": "scenario",
                    "polarity": "prefer",
                    "subject": "tone",
                    "content": "warmer scenario narration",
                }
            ]
        )
    )

    assert approval_gate.is_stage_approved("scenario") is False

    r = client.post(
        "/api/directive",
        json={
            "directive": "warmer overall",
            "reviewer": "alice",
            "l4_event_id": "L4-stage-001",
            "slot_context": {"stage": "scenario"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope_hint"] == {
        "scope": "stage",
        "scope_ref": "scenario",
    }
    assert body["released_stage"] == "scenario"
    assert body["status"] == "accepted"
    assert len(body["record_ids"]) >= 1

    # The gate flag flipped -- wait_for_approval on the same stage
    # returns immediately and the close event carries decision="approved".
    assert approval_gate.is_stage_approved("scenario") is True


def test_non_stage_scoped_directive_does_not_release_gate(
    client, output_dir
):
    """A scene-scoped directive must NOT touch the approval gate.  The
    gate is a *stage* boundary; narrower scopes drift the stage in place
    without unblocking it."""
    set_llm_client_factory(
        _stub_llm_records(
            [
                {
                    "scope": "scene",
                    "scope_ref": "scene-3",
                    "polarity": "prefer",
                    "subject": "pacing",
                    "content": "tighter in scene 3",
                }
            ]
        )
    )

    assert approval_gate.is_stage_approved("scenario") is False

    r = client.post(
        "/api/directive",
        json={
            "directive": "tighter scene 3",
            "reviewer": "alice",
            "l4_event_id": "L4-scene-001",
            "slot_context": {"scene_num": 3},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope_hint"] == {"scope": "scene", "scope_ref": "scene-3"}
    assert body["released_stage"] is None
    assert approval_gate.is_stage_approved("scenario") is False


def test_stage_scoped_directive_closes_inline_gate_loop(
    client, output_dir, monkeypatch
):
    """End-to-end: a blocking ``wait_for_approval`` in a worker thread is
    released when a stage-scoped directive posts through the HTTP API."""
    monkeypatch.setattr(approval_gate, "_MAX_WAIT", 2.0)
    monkeypatch.setattr(approval_gate, "_POLL_INTERVAL", 0.02)

    set_llm_client_factory(
        _stub_llm_records(
            [
                {
                    "scope": "stage",
                    "scope_ref": "clips",
                    "polarity": "avoid",
                    "subject": "visual_style",
                    "content": "no lens flare in the clip stage",
                }
            ]
        )
    )

    stage = "clips"
    queue = agui.subscribe_agui_events()
    result: dict[str, Any] = {}

    def _wait():
        result["approved"] = approval_gate.wait_for_approval(stage)

    worker = threading.Thread(target=_wait, daemon=True)
    worker.start()

    # Give the poll loop a couple of ticks to emit the open event, then
    # post the stage-scoped directive.
    time.sleep(0.1)

    r = client.post(
        "/api/directive",
        json={
            "directive": "no lens flare",
            "reviewer": "alice",
            "l4_event_id": "L4-clips-001",
            "slot_context": {"stage": stage},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["released_stage"] == stage

    worker.join(timeout=2.0)
    events = _drain(queue)
    agui.unsubscribe_agui_events(queue)

    assert result.get("approved") is True, (
        "wait_for_approval did not exit after stage-scoped directive: "
        f"{result}"
    )

    opens = [e for e in events if e["type"] == "approval_gate_opened"]
    closes = [e for e in events if e["type"] == "approval_gate_closed"]
    assert len(opens) == 1
    assert len(closes) == 1
    assert closes[0]["data"]["stage"] == stage
    assert closes[0]["data"]["decision"] == "approved"
