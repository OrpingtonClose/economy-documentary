"""Tests for the UI-04 aggregation endpoints (#201 and #204).

``GET /api/slots/{slot_id}/full`` is the single-fetch aggregator the
rebuilt right-rail slot panel consumes. ``GET /api/reasoning/raw`` is
the advanced-mode raw trace feed, filtered by ``slot_id``.

These tests drive both endpoints via ``fastapi.testclient.TestClient``,
seeding a temporary ``PIPELINE_OUTPUT_DIR`` with the minimal on-disk
fixtures the underlying read-model expects.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import agui  # noqa: E402
import dashboard_directives as dd  # noqa: E402
from callbacks.preference_ledger import (  # noqa: E402
    Origin,
    Polarity,
    Scope,
    Subject,
    append_preference,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    """Redirect every disk-backed store into ``tmp_path``."""
    monkeypatch.setenv("PIPELINE_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(agui, "_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(dd, "_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        dd, "_HALT_FILE", os.path.join(str(tmp_path), ".halt_state.json")
    )
    monkeypatch.setattr(
        dd,
        "_BLACKBOARD_FILE",
        os.path.join(str(tmp_path), ".dashboard_blackboard.json"),
    )
    return tmp_path


@pytest.fixture
def reasoning_db(tmp_path, monkeypatch):
    """Redirect the reasoning-trace SQLite DB to a throwaway path."""
    db_path = tmp_path / "reasoning_traces.db"

    from plugins import reasoning_trace as rt_module

    monkeypatch.setattr(rt_module, "_REASONING_DB", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reasoning_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   REAL    NOT NULL,
            event_type  TEXT    NOT NULL,
            agent_name  TEXT    NOT NULL DEFAULT '',
            model       TEXT    NOT NULL DEFAULT '',
            content     TEXT    NOT NULL DEFAULT '',
            tokens_in   INTEGER,
            tokens_out  INTEGER,
            metadata    TEXT    NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def client(output_dir, reasoning_db):
    app = FastAPI()
    app.include_router(agui.router)
    app.include_router(agui.api_router)
    return TestClient(app)


def _seed_blackboard(tmp_path: Path, state: dict) -> None:
    path = os.path.join(str(tmp_path), ".dashboard_blackboard.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def _insert_reasoning(
    db_path: Path,
    *,
    event_type: str,
    agent_name: str,
    content: str,
    metadata: dict | None = None,
    offset: float = 0.0,
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            """
            INSERT INTO reasoning_log
                (timestamp, event_type, agent_name, model, content,
                 tokens_in, tokens_out, metadata)
            VALUES (?, ?, ?, '', ?, NULL, NULL, ?)
            """,
            (
                time.time() + offset,
                event_type,
                agent_name,
                content,
                json.dumps(metadata or {}),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /api/slots/{slot_id}/full
# ---------------------------------------------------------------------------


def test_slot_full_rejects_malformed_id(client):
    r = client.get("/api/slots/not-a-slot/full")
    assert r.status_code == 400
    assert "malformed" in r.json()["error"]


def test_slot_full_empty_pipeline_returns_consistent_shape(client):
    r = client.get("/api/slots/V1:1:1/full")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "slot",
        "takes",
        "critiques",
        "qa_results",
        "artifacts",
        "ledger_records",
        "reasoning_trace_preview",
    ):
        assert key in body, f"missing key {key!r} in response"
    assert body["slot"]["slot_id"] == "V1:1:1"
    assert body["slot"]["scene_num"] == 1
    assert body["slot"]["phrase_idx"] == 1
    assert isinstance(body["takes"], list)
    assert isinstance(body["critiques"], list)
    assert isinstance(body["qa_results"], list)
    assert isinstance(body["ledger_records"], list)
    assert isinstance(body["reasoning_trace_preview"], list)


def test_slot_full_surfaces_scope_resolved_ledger_records(
    client, output_dir
):
    """Global + scene-1 records apply; scene-2 records do not."""
    state: dict = {}
    append_preference(
        state,
        scope=Scope.GLOBAL,
        scope_ref=None,
        polarity=Polarity.PREFER,
        subject=Subject.TONE,
        content="warm overall tone",
        origin=Origin(
            l4_event_id="R0",
            reviewer="tester",
            timestamp="2025-01-01T00:00:00Z",
        ),
    )
    append_preference(
        state,
        scope=Scope.SCENE,
        scope_ref="1",
        polarity=Polarity.REQUIRE,
        subject=Subject.PACING,
        content="slow pacing",
        origin=Origin(
            l4_event_id="L4-1",
            reviewer="tester",
            timestamp="2025-01-02T00:00:00Z",
        ),
    )
    append_preference(
        state,
        scope=Scope.SCENE,
        scope_ref="2",
        polarity=Polarity.PREFER,
        subject=Subject.TONE,
        content="scene 2 only",
        origin=Origin(
            l4_event_id="L4-2",
            reviewer="tester",
            timestamp="2025-01-03T00:00:00Z",
        ),
    )
    _seed_blackboard(output_dir, state)

    r = client.get("/api/slots/V1:1:1/full")
    assert r.status_code == 200
    contents = {rec["content"] for rec in r.json()["ledger_records"]}
    assert "warm overall tone" in contents
    assert "slow pacing" in contents
    assert "scene 2 only" not in contents


def test_slot_full_builds_takes_from_artifact_history(client, output_dir):
    """Artifact history from the feedback store shows up as ordered takes."""
    agui._store.register_artifact(
        agui.ArtifactEvent(
            id="art-1",
            artifact_type=agui.ArtifactType.VIDEO_CLIP,
            status=agui.ArtifactStatus.REJECTED,
            scene_num=1,
            phrase_idx=1,
            preview_url="https://example.com/v1.mp4",
            timestamp=1.0,
        )
    )
    agui._store.register_artifact(
        agui.ArtifactEvent(
            id="art-2",
            artifact_type=agui.ArtifactType.VIDEO_CLIP,
            status=agui.ArtifactStatus.APPROVED,
            scene_num=1,
            phrase_idx=1,
            preview_url="https://example.com/v2.mp4",
            timestamp=2.0,
        )
    )
    try:
        r = client.get("/api/slots/V1:1:1/full")
        assert r.status_code == 200
        body = r.json()
        takes = body["takes"]
        assert len(takes) >= 2
        # Ascending by timestamp: first is rejected, last is accepted.
        outcomes = [t["outcome"] for t in takes]
        assert "rejected" in outcomes
        assert "accepted" in outcomes
        # Each take has an artifact id + preview url.
        for take in takes:
            assert "revision" in take
            assert "preview_url" in take
    finally:
        # Leave the in-memory store clean for subsequent tests.
        agui._store._artifacts.clear()


def test_slot_full_reasoning_trace_preview_is_bounded(
    client, output_dir, tmp_path, monkeypatch
):
    """``reasoning_trace_preview`` is capped at 20 matching digest entries."""
    from plugins import reasoning_digest as rd

    # Redirect the digest store to a throwaway SQLite file so we do not
    # collide with any real digests on the dev machine.
    db_path = tmp_path / "reasoning_digests.db"
    monkeypatch.setattr(rd, "_DIGEST_DB", str(db_path))
    # Reset the singleton so it re-opens against the patched path.
    monkeypatch.setattr(rd, "_engine", None)

    engine = rd.get_digest_engine()
    for i in range(30):
        engine._store.write(  # type: ignore[attr-defined]
            rd.Digest(
                timestamp=time.time() + i,
                agent="narration_writer",
                phase="scenario",
                importance=rd.Importance.MEDIUM,
                summary=f"scene 1 phrase 1 narration draft iteration {i}",
                details={"iteration": i},
                raw_trace_ids=[],
            )
        )

    r = client.get("/api/slots/A1:1:1/full")
    assert r.status_code == 200
    preview = r.json()["reasoning_trace_preview"]
    assert len(preview) <= 20


# ---------------------------------------------------------------------------
# /api/reasoning/raw
# ---------------------------------------------------------------------------


def test_reasoning_raw_without_slot_returns_most_recent(client, reasoning_db):
    for idx in range(5):
        _insert_reasoning(
            reasoning_db,
            event_type="llm_response",
            agent_name=f"agent-{idx}",
            content=f"some reasoning {idx}",
            offset=float(idx),
        )
    r = client.get("/api/reasoning/raw?limit=3")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert len(body["traces"]) == 3


def test_reasoning_raw_filters_by_slot_id(client, reasoning_db):
    _insert_reasoning(
        reasoning_db,
        event_type="llm_response",
        agent_name="video_planner",
        content="planning scene 1 phrase 1 video content",
        offset=1.0,
    )
    _insert_reasoning(
        reasoning_db,
        event_type="llm_response",
        agent_name="narration_writer",
        content="scene 2 phrase 1 narration drafting",
        offset=2.0,
    )
    _insert_reasoning(
        reasoning_db,
        event_type="llm_response",
        agent_name="video_planner",
        content="working on V1:1:1 iteration",
        offset=3.0,
    )

    r = client.get("/api/reasoning/raw?slot_id=V1:1:1&limit=50")
    assert r.status_code == 200
    body = r.json()
    # Both the "scene 1 phrase 1 video" and "V1:1:1" entries match.
    contents = [t["content"] for t in body["traces"]]
    assert any("scene 1 phrase 1 video" in c for c in contents)
    assert any("V1:1:1" in c for c in contents)
    # The narration-scoped entry must not be returned for a video slot.
    assert not any("scene 2 phrase 1 narration" in c for c in contents)


def test_reasoning_raw_rejects_malformed_slot_id(client):
    r = client.get("/api/reasoning/raw?slot_id=not-a-slot")
    assert r.status_code == 400


def test_reasoning_raw_limit_is_clamped(client, reasoning_db):
    r = client.get("/api/reasoning/raw?limit=999999")
    assert r.status_code == 200
    # ``limit`` is clamped to 1000 internally; we can only observe that
    # the query does not error and returns at most that many rows.
    assert r.json()["count"] <= 1000


def test_reasoning_raw_returns_chronological_order(client, reasoning_db):
    ids = []
    for idx in range(5):
        ids.append(
            _insert_reasoning(
                reasoning_db,
                event_type="llm_response",
                agent_name="agent",
                content=f"scene 1 phrase 1 entry {idx}",
                offset=float(idx),
            )
        )
    r = client.get("/api/reasoning/raw?slot_id=V1:1:1&limit=100")
    assert r.status_code == 200
    body = r.json()
    timestamps = [t["timestamp"] for t in body["traces"]]
    assert timestamps == sorted(timestamps)
