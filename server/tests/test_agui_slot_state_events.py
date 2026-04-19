"""
Tests for the ARCH-H1 slot-state SSE bridge and the ARCH-H2 authoritative
event.

These assert the *emission* contract: every ``ArtifactEvent`` that flows
through :class:`FeedbackStore` produces a ``slot_state`` event on the
shared bus, and crystallising the OTIO state to ``authoritative`` emits
an ``otio_authoritative`` event.  The dashboard binds to both so it can
drive the centrepiece timeline without polling.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from agui import (  # noqa: E402
    ArtifactEvent,
    ArtifactStatus,
    ArtifactType,
    FeedbackStore,
    emit_otio_authoritative,
    subscribe_agui_events,
    unsubscribe_agui_events,
)


def _drain(queue) -> list[dict]:
    out: list[dict] = []
    while queue:
        out.append(queue.popleft())
    return out


def test_register_artifact_emits_slot_state():
    store = FeedbackStore()
    queue = subscribe_agui_events()
    try:
        store.register_artifact(ArtifactEvent(
            id="art-1",
            artifact_type=ArtifactType.VIDEO_CLIP,
            status=ArtifactStatus.GENERATING,
            scene_num=2,
            phrase_idx=3,
            preview_url="",
            duration_sec=4.1,
            timestamp=0.0,
        ))
        events = _drain(queue)
    finally:
        unsubscribe_agui_events(queue)

    types = [e["type"] for e in events]
    assert "artifact" in types
    assert "slot_state" in types
    slot_event = next(e for e in events if e["type"] == "slot_state")
    assert slot_event["data"]["slot_id"] == "V1:2:3"
    assert slot_event["data"]["track"] == "V1_Video"
    assert slot_event["data"]["status"] == "in_progress"


def test_update_artifact_status_emits_slot_state():
    store = FeedbackStore()
    store.register_artifact(ArtifactEvent(
        id="art-n",
        artifact_type=ArtifactType.NARRATION,
        status=ArtifactStatus.GENERATING,
        scene_num=1,
        phrase_idx=1,
        preview_url="",
        duration_sec=3.0,
        timestamp=0.0,
    ))
    queue = subscribe_agui_events()
    try:
        store.update_artifact_status("art-n", ArtifactStatus.APPROVED)
        events = _drain(queue)
    finally:
        unsubscribe_agui_events(queue)
    slot_events = [e for e in events if e["type"] == "slot_state"]
    assert any(
        e["data"]["slot_id"] == "A1:1:1" and e["data"]["status"] == "delivered"
        for e in slot_events
    )


def test_non_media_artifact_type_does_not_emit_slot_state():
    """Scene scripts / visual concepts / assembled-video artifacts are not
    slots on the centrepiece timeline — they must not emit slot_state."""
    store = FeedbackStore()
    queue = subscribe_agui_events()
    try:
        store.register_artifact(ArtifactEvent(
            id="script-1",
            artifact_type=ArtifactType.SCENE_SCRIPT,
            status=ArtifactStatus.APPROVED,
            scene_num=1,
            phrase_idx=0,
            preview_url="",
            duration_sec=0.0,
            timestamp=0.0,
        ))
        events = _drain(queue)
    finally:
        unsubscribe_agui_events(queue)
    assert not any(e["type"] == "slot_state" for e in events)


def test_emit_otio_authoritative_reaches_subscribers():
    queue = subscribe_agui_events()
    try:
        emit_otio_authoritative(timeline_path="/tmp/t.otio", reason="end_of_audio")
        events = _drain(queue)
    finally:
        unsubscribe_agui_events(queue)
    assert any(
        e["type"] == "otio_authoritative"
        and e["data"]["timeline_path"] == "/tmp/t.otio"
        for e in events
    )
