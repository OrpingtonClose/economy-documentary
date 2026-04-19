"""
Unit tests for ARCH-H5 (issue #160) -- the reasoning digest writer.

Parent: ARCH-H #130. Meta: ARCH-2026 #122.

Covers the invariants declared in :mod:`dashboard.reasoning_digest`:

1. Deterministic rule-based summariser (no LLM), one rule per event kind.
2. Non-blocking SSE emission.
3. Truncation rule preserves the informative prefix.
4. Fail-loud on unknown event kind.
5. Blackboard log append via the ``reasoning_digest_log`` output_key.
"""

from __future__ import annotations

import collections
import json
import sys
import threading
import time
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from dashboard.reasoning_digest import (  # noqa: E402
    EVENT_KINDS,
    MAX_SUMMARY_CHARS,
    REASONING_DIGEST_LOG_KEY,
    ReasoningDigest,
    SCOPES,
    emit_digest,
    get_digest_log,
    subscribe_digest_stream,
    summarise_event,
    unsubscribe_digest_stream,
    write_digest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_subscriber():
    """A subscriber queue that is auto-cleaned after the test."""
    queue = subscribe_digest_stream()
    yield queue
    unsubscribe_digest_stream(queue)


# ---------------------------------------------------------------------------
# 1. Per-event-kind rules (one test per entry in EVENT_KINDS)
# ---------------------------------------------------------------------------


def test_event_kinds_closed_vocabulary() -> None:
    """The module's closed vocabulary is exactly the set ARCH-H5 specifies."""
    assert set(EVENT_KINDS) == {
        "stage_start",
        "stage_end",
        "gate_open",
        "gate_close",
        "preview_built",
        "ledger_change",
        "ladder_step",
        "qa_verdict",
        "infra_event",
        "eta_revision",
    }


def test_rule_stage_start() -> None:
    digest = summarise_event("stage_start", {"stage": "scenario"})
    assert digest.kind == "stage_start"
    assert digest.scope == "stage"
    assert "scenario" in digest.summary
    assert digest.summary.startswith("Stage 'scenario'")


def test_rule_stage_end_includes_status_and_duration() -> None:
    digest = summarise_event(
        "stage_end",
        {"stage": "audio", "status": "ok", "duration_sec": 12.34},
    )
    assert digest.kind == "stage_end"
    assert digest.scope == "stage"
    assert digest.summary.startswith("Stage 'audio'")
    assert "status=ok" in digest.summary
    assert "12.3s" in digest.summary


def test_rule_gate_open() -> None:
    digest = summarise_event("gate_open", {"stage": "clips"})
    assert digest.kind == "gate_open"
    assert digest.scope == "stage"
    assert "clips" in digest.summary
    assert "awaiting human review" in digest.summary


def test_rule_gate_close_records_reviewer_and_decision() -> None:
    digest = summarise_event(
        "gate_close",
        {"stage": "clips", "decision": "rejected", "reviewer": "operator"},
    )
    assert digest.kind == "gate_close"
    assert digest.scope == "stage"
    assert "clips" in digest.summary
    assert "rejected" in digest.summary
    assert "operator" in digest.summary


def test_rule_preview_built_scene_scope() -> None:
    digest = summarise_event(
        "preview_built",
        {
            "artifact_type": "scene_preview",
            "scene_num": 4,
            "preview_url": "https://b2.example/preview.mp4",
            "duration_sec": 7.0,
        },
    )
    assert digest.kind == "preview_built"
    assert digest.scope == "scene"
    assert "scene 4" in digest.summary
    assert "scene_preview" in digest.summary
    assert "https://b2.example/preview.mp4" in digest.summary


def test_rule_preview_built_clip_scope_when_no_scene_num() -> None:
    digest = summarise_event(
        "preview_built",
        {"artifact_type": "clip_preview", "preview_url": "/tmp/x.mp4"},
    )
    assert digest.scope == "clip"


def test_rule_ledger_change_uses_revision_and_content() -> None:
    digest = summarise_event(
        "ledger_change",
        {
            "revision": 7,
            "scope": "scene",
            "scope_ref": "scene-03",
            "polarity": "prefer",
            "subject": "tone",
            "content": "warmer narration",
        },
    )
    assert digest.kind == "ledger_change"
    assert digest.scope == "scene"
    assert "R7" in digest.summary
    assert "prefer" in digest.summary
    assert "tone" in digest.summary
    assert "scene-03" in digest.summary
    assert "warmer narration" in digest.summary


def test_rule_ledger_change_global_scope_has_global_digest_scope() -> None:
    digest = summarise_event(
        "ledger_change",
        {
            "revision": 1,
            "scope": "global",
            "polarity": "require",
            "subject": "language",
            "content": "en-US",
        },
    )
    assert digest.scope == "global"


def test_rule_ladder_step_records_level_action_and_operation() -> None:
    digest = summarise_event(
        "ladder_step",
        {
            "level": 2,
            "level_name": "CREATIVE",
            "operation": "tts_generation",
            "action": "fix",
            "explanation": "reduced speech rate by 15%",
            "success": True,
        },
    )
    assert digest.kind == "ladder_step"
    assert digest.scope == "stage"
    assert "L2" in digest.summary
    assert "CREATIVE" in digest.summary
    assert "tts_generation" in digest.summary
    assert "fix" in digest.summary
    assert "succeeded" in digest.summary
    assert "reduced speech rate" in digest.summary


def test_rule_qa_verdict_maps_artifact_to_scope() -> None:
    digest = summarise_event(
        "qa_verdict",
        {
            "source": "gatekeeper",
            "check_name": "duration_match",
            "verdict": "fail",
            "message": "clip too long",
            "artifact_type": "clip",
            "artifact_id": "clip-42",
        },
    )
    assert digest.kind == "qa_verdict"
    assert digest.scope == "clip"
    assert "gatekeeper" in digest.summary
    assert "duration_match" in digest.summary
    assert "fail" in digest.summary
    assert "clip:clip-42" in digest.summary
    assert "clip too long" in digest.summary


def test_rule_infra_event_is_global_scope() -> None:
    digest = summarise_event(
        "infra_event",
        {
            "event": "vm_provisioned",
            "worker": "video-worker-1",
            "level": 0,
            "detail": "fresh ComfyUI VM",
        },
    )
    assert digest.kind == "infra_event"
    assert digest.scope == "global"
    assert "vm_provisioned" in digest.summary
    assert "video-worker-1" in digest.summary
    assert "L0" in digest.summary
    assert "fresh ComfyUI VM" in digest.summary


def test_rule_eta_revision_renders_arrow() -> None:
    digest = summarise_event(
        "eta_revision",
        {
            "stage": "video",
            "old_eta_sec": 300,
            "new_eta_sec": 450,
            "reason": "worker recycled",
        },
    )
    assert digest.kind == "eta_revision"
    assert digest.scope == "stage"
    assert "video" in digest.summary
    assert "300s" in digest.summary
    assert "450s" in digest.summary
    assert "worker recycled" in digest.summary


def test_every_event_kind_has_a_rule() -> None:
    """No kind in the closed vocabulary is silently unhandled."""
    for kind in EVENT_KINDS:
        # Minimal event -- every rule must tolerate sparse input.
        digest = summarise_event(kind, {})
        assert digest.kind == kind
        assert digest.scope in SCOPES
        assert digest.summary  # non-empty
        assert len(digest.summary) <= MAX_SUMMARY_CHARS


# ---------------------------------------------------------------------------
# 2. Truncation rule
# ---------------------------------------------------------------------------


def test_truncation_rule_keeps_informative_prefix() -> None:
    long_content = "x" * (MAX_SUMMARY_CHARS * 3)
    digest = summarise_event(
        "ledger_change",
        {
            "revision": 99,
            "scope": "global",
            "polarity": "require",
            "subject": "tone",
            "content": long_content,
        },
    )
    assert len(digest.summary) <= MAX_SUMMARY_CHARS
    # Prefix preserved so the dashboard can still identify what this is about.
    assert digest.summary.startswith("Ledger R99:")
    assert digest.summary.endswith("\u2026")


def test_truncation_rule_does_not_truncate_short_summaries() -> None:
    digest = summarise_event("stage_start", {"stage": "audio"})
    assert digest.summary == "Stage 'audio' started."
    assert "\u2026" not in digest.summary


# ---------------------------------------------------------------------------
# 3. Fail-loud on unknown kind
# ---------------------------------------------------------------------------


def test_summarise_unknown_kind_raises() -> None:
    with pytest.raises(ValueError) as excinfo:
        summarise_event("definitely_not_a_real_kind", {"foo": "bar"})
    assert "unknown reasoning-digest event kind" in str(excinfo.value)


def test_summarise_requires_mapping_event() -> None:
    with pytest.raises(TypeError):
        summarise_event("stage_start", "not a mapping")  # type: ignore[arg-type]


def test_emit_digest_unknown_kind_raises() -> None:
    state: dict = {}
    with pytest.raises(ValueError):
        emit_digest(state, "not_real", {})
    # Nothing should have been appended when the summariser refused.
    assert REASONING_DIGEST_LOG_KEY not in state or state[REASONING_DIGEST_LOG_KEY] == []


# ---------------------------------------------------------------------------
# 4. SSE emission -- non-blocking
# ---------------------------------------------------------------------------


def test_emit_digest_pushes_onto_subscriber_queue(fresh_subscriber) -> None:
    emit_digest(None, "stage_start", {"stage": "scenario"})
    assert len(fresh_subscriber) == 1
    payload = fresh_subscriber.popleft()
    assert payload["kind"] == "stage_start"
    assert payload["scope"] == "stage"
    assert payload["source_event"] == {"stage": "scenario"}
    assert payload["summary"].startswith("Stage 'scenario'")


def test_emit_digest_does_not_block_when_many_subscribers_present() -> None:
    """Emission must be O(N subscribers) with no locking on the caller path.

    We register a pathological number of subscribers and measure wall-clock
    emission time: a single digest must dispatch in well under a second
    even with 256 subscribers, demonstrating that the bus does no I/O and
    does not await slow consumers.
    """
    subs = [subscribe_digest_stream() for _ in range(256)]
    try:
        start = time.monotonic()
        emit_digest(None, "stage_end", {"stage": "audio", "status": "ok"})
        elapsed = time.monotonic() - start
    finally:
        for q in subs:
            unsubscribe_digest_stream(q)

    # Generous bound -- in practice this completes in < 10 ms.
    assert elapsed < 0.5, f"emit_digest blocked for {elapsed:.3f}s"
    for q in subs:
        assert len(q) == 1


def test_emit_digest_does_not_block_on_slow_consumer() -> None:
    """A subscriber that never drains its queue must not stall the writer.

    We install a single subscriber and never read from it; the writer
    then emits many digests.  The bounded deque drops old events when
    full, so memory does not grow without bound, and the writer returns
    promptly every time.
    """
    queue = subscribe_digest_stream()
    try:
        start = time.monotonic()
        for i in range(10_000):
            emit_digest(None, "stage_start", {"stage": f"s{i}"})
        elapsed = time.monotonic() - start
    finally:
        unsubscribe_digest_stream(queue)

    # 10k emissions should finish in well under a second.  The deque is
    # bounded so it never grows past its maxlen even though nobody drains.
    assert elapsed < 5.0, f"10k emissions took {elapsed:.3f}s"
    assert len(queue) <= queue.maxlen  # type: ignore[operator]


def test_emit_digest_is_thread_safe(fresh_subscriber) -> None:
    """Concurrent writers must not corrupt the subscriber queue or log."""
    state: dict = {}
    event_kind = "stage_start"

    def writer(thread_idx: int) -> None:
        for j in range(50):
            emit_digest(state, event_kind, {"stage": f"t{thread_idx}-{j}"})

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 8 threads x 50 writes = 400 digests.  Bus may coalesce but neither
    # the subscriber queue nor the blackboard log should blow up.
    assert len(fresh_subscriber) == 400
    assert len(get_digest_log(state)) == 400


# ---------------------------------------------------------------------------
# 5. Blackboard log
# ---------------------------------------------------------------------------


def test_write_digest_appends_to_blackboard_log() -> None:
    state: dict = {}
    digest = ReasoningDigest(
        timestamp=time.time(),
        kind="stage_start",
        scope="stage",
        summary="Stage 'scenario' started.",
        source_event={"stage": "scenario"},
    )
    write_digest(state, digest)
    assert state[REASONING_DIGEST_LOG_KEY] == [digest.to_dict()]

    write_digest(state, digest)
    assert len(state[REASONING_DIGEST_LOG_KEY]) == 2


def test_write_digest_accepts_json_string_storage() -> None:
    """Some ADK ``output_key`` sites serialise state as a JSON string."""
    state: dict = {REASONING_DIGEST_LOG_KEY: "[]"}
    emit_digest(state, "stage_start", {"stage": "scenario"})
    assert isinstance(state[REASONING_DIGEST_LOG_KEY], str)
    decoded = json.loads(state[REASONING_DIGEST_LOG_KEY])
    assert len(decoded) == 1
    assert decoded[0]["kind"] == "stage_start"


def test_write_digest_allows_none_state() -> None:
    """Emission-only mode: no blackboard, SSE only."""
    queue = subscribe_digest_stream()
    try:
        emit_digest(None, "gate_open", {"stage": "clips"})
    finally:
        unsubscribe_digest_stream(queue)
    assert len(queue) == 1


def test_get_digest_log_rejects_malformed_storage() -> None:
    state = {REASONING_DIGEST_LOG_KEY: 12345}
    with pytest.raises(TypeError):
        get_digest_log(state)

    state = {REASONING_DIGEST_LOG_KEY: "{not json}"}
    with pytest.raises(ValueError):
        get_digest_log(state)


# ---------------------------------------------------------------------------
# 6. Subscribe / unsubscribe lifecycle
# ---------------------------------------------------------------------------


def test_unsubscribe_is_idempotent() -> None:
    queue = subscribe_digest_stream()
    unsubscribe_digest_stream(queue)
    unsubscribe_digest_stream(queue)  # must not raise


def test_unsubscribed_queue_stops_receiving() -> None:
    queue = subscribe_digest_stream()
    unsubscribe_digest_stream(queue)
    emit_digest(None, "stage_start", {"stage": "scenario"})
    assert not queue


def test_subscriber_queue_is_bounded_deque() -> None:
    queue = subscribe_digest_stream()
    try:
        assert isinstance(queue, collections.deque)
        assert queue.maxlen is not None and queue.maxlen > 0
    finally:
        unsubscribe_digest_stream(queue)
