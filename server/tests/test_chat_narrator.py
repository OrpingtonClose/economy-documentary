"""Unit tests for the UI-01 chat narrator (issue #186 + children).

Covers:

* UI-01a (#193) — promotion filter rules (type match, opt-in flag,
  suppressing tags, dedup window).
* UI-01b (#194) — plain-English templates, one snapshot per kind.
* UI-01c (#195) — ``[[slot:ID]]`` / ``[[preview:BOUND]]`` token round-trip.
* Pipeline-safety invariant — emission is non-blocking even for a wedged
  subscriber queue.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from agents.chat_narrator import (  # noqa: E402
    DEDUP_WINDOW_SEC,
    NARRATOR_EVENT_KINDS,
    Narrator,
    NarratorEvent,
    SUPPRESSING_TAGS,
    bridge_from_reasoning_digest,
    emit_narrator_event,
    format_turn,
    get_narrator,
    preview_token,
    should_promote,
    slot_token,
    subscribe_narrator_events,
    unsubscribe_narrator_events,
)


# ---------------------------------------------------------------------------
# Closed vocabulary — fail-loud invariant
# ---------------------------------------------------------------------------


def test_narrator_event_kinds_closed_vocabulary() -> None:
    """The public kind list is exactly the set #194 specifies."""
    assert set(NARRATOR_EVENT_KINDS) == {
        "stage_started",
        "stage_completed",
        "approval_gate_opened",
        "take_failed",
        "take_retried",
        "reconciliation_converged",
        "preview_ready",
        "directive_applied",
        "halt_fired",
    }


def test_format_turn_unknown_kind_fails_loud() -> None:
    with pytest.raises(ValueError, match="unknown narrator event kind"):
        format_turn(NarratorEvent(kind="not_a_real_kind"))


# ---------------------------------------------------------------------------
# UI-01a — promotion filter rules
# ---------------------------------------------------------------------------


class TestShouldPromote:
    def test_known_kind_promotes_without_flag(self) -> None:
        for kind in NARRATOR_EVENT_KINDS:
            assert should_promote(kind) is True, kind

    def test_unknown_kind_requires_opt_in_flag(self) -> None:
        assert should_promote("heartbeat") is False
        assert should_promote("heartbeat", promote_to_chat=True) is True

    @pytest.mark.parametrize("tag", sorted(SUPPRESSING_TAGS))
    def test_internal_or_debug_tag_always_suppresses(self, tag: str) -> None:
        assert should_promote("stage_started", tags=[tag]) is False
        assert (
            should_promote("heartbeat", tags=[tag], promote_to_chat=True)
            is False
        )

    def test_irrelevant_tag_has_no_effect(self) -> None:
        assert should_promote("stage_started", tags=["audio"]) is True


# ---------------------------------------------------------------------------
# UI-01b — plain-English templates (snapshot per kind)
# ---------------------------------------------------------------------------


class TestFormatTurn:
    """Snapshot tests for every narrator kind.

    When a template changes intentionally, update these strings in the
    same commit so a rename never silently changes user-facing chat.
    """

    def test_stage_started_uses_human_stage_name(self) -> None:
        text = format_turn(NarratorEvent(kind="stage_started", fields={"stage": "audio"}))
        assert text == "Starting audio\u2026"

    def test_stage_started_unknown_stage_passes_through(self) -> None:
        text = format_turn(
            NarratorEvent(kind="stage_started", fields={"stage": "stitching"})
        )
        assert text == "Starting stitching\u2026"

    def test_stage_started_missing_stage(self) -> None:
        text = format_turn(NarratorEvent(kind="stage_started", fields={}))
        assert text == "Starting the pipeline\u2026"

    def test_stage_completed(self) -> None:
        text = format_turn(
            NarratorEvent(kind="stage_completed", fields={"stage": "assembly"})
        )
        assert text == "Assembly complete."

    def test_stage_completed_visual_direction(self) -> None:
        text = format_turn(
            NarratorEvent(
                kind="stage_completed", fields={"stage": "visual_direction"}
            )
        )
        # "visual direction" gets the first letter capitalised but the
        # embedded lowercase words stay as-is (no title-case).
        assert text == "Visual direction complete."

    def test_approval_gate_opened(self) -> None:
        text = format_turn(
            NarratorEvent(
                kind="approval_gate_opened", fields={"stage": "scenario"}
            )
        )
        assert text == (
            "scenario ready \u2014 approve to proceed, or reject with a note."
        )

    def test_take_failed_emits_slot_chip_token(self) -> None:
        text = format_turn(
            NarratorEvent(
                kind="take_failed",
                fields={
                    "slot_id": "A1:3:0",
                    "qa_axis": "loudness",
                    "reason": "too quiet",
                },
            )
        )
        assert text == "[[slot:A1:3:0]] failed loudness (too quiet) \u2014 retrying."

    def test_take_retried(self) -> None:
        text = format_turn(
            NarratorEvent(
                kind="take_retried",
                fields={"slot_id": "V1:2:1", "n": 2, "change": "higher denoise"},
            )
        )
        assert text == "[[slot:V1:2:1]] take 2 retrying with higher denoise."

    def test_reconciliation_converged(self) -> None:
        text = format_turn(
            NarratorEvent(
                kind="reconciliation_converged",
                fields={"duration_sec": 72.345},
            )
        )
        assert text == "Narration locked at 72.3s \u2014 within tolerance."

    def test_preview_ready_emits_preview_token(self) -> None:
        text = format_turn(
            NarratorEvent(
                kind="preview_ready",
                fields={"boundary": "scenario", "duration_sec": 9.6},
            )
        )
        assert text == "[[preview:scenario]] ready \u2014 10s."

    def test_directive_applied_pluralises_correctly(self) -> None:
        text = format_turn(
            NarratorEvent(
                kind="directive_applied",
                fields={"directive_text": "warmer narration", "n_drifted": 3},
            )
        )
        assert text == "Applied 'warmer narration'; 3 slots will re-run."

    def test_directive_applied_singular(self) -> None:
        text = format_turn(
            NarratorEvent(
                kind="directive_applied",
                fields={"directive_text": "tighter pace", "n_drifted": 1},
            )
        )
        assert text == "Applied 'tighter pace'; 1 slot will re-run."

    def test_halt_fired(self) -> None:
        text = format_turn(
            NarratorEvent(
                kind="halt_fired",
                fields={"stage": "audio", "checkpoint": "post-tts-gate"},
            )
        )
        assert text == "Paused at audio. Last safe checkpoint was post-tts-gate."


# ---------------------------------------------------------------------------
# UI-01c — slot/preview token round-trip
# ---------------------------------------------------------------------------


class TestTokenRoundTrip:
    @pytest.mark.parametrize(
        "slot_id",
        ["A1:3:0", "V1:12:4", "audio-scene-1-block-2", "bare_id"],
    )
    def test_slot_id_round_trip(self, slot_id: str) -> None:
        token = slot_token(slot_id)
        assert token == f"[[slot:{slot_id}]]"
        event = NarratorEvent(
            kind="take_failed",
            fields={
                "slot_id": slot_id,
                "qa_axis": "x",
                "reason": "y",
            },
        )
        assert token in format_turn(event)

    @pytest.mark.parametrize(
        "boundary",
        ["scenario", "audio", "act-1", "final_cut"],
    )
    def test_preview_boundary_round_trip(self, boundary: str) -> None:
        token = preview_token(boundary)
        assert token == f"[[preview:{boundary}]]"
        event = NarratorEvent(
            kind="preview_ready",
            fields={"boundary": boundary, "duration_sec": 10},
        )
        assert token in format_turn(event)


# ---------------------------------------------------------------------------
# Narrator.emit — filter + dedup + fan-out
# ---------------------------------------------------------------------------


class TestNarratorEmit:
    def test_suppressed_kind_not_published(self) -> None:
        n = Narrator()
        q = n.subscribe()
        try:
            result = n.emit(
                "stage_started",
                fields={"stage": "audio"},
                tags=["internal"],
            )
            assert result is None
            assert len(q) == 0
        finally:
            n.unsubscribe(q)

    def test_unknown_kind_without_flag_dropped(self) -> None:
        n = Narrator()
        q = n.subscribe()
        try:
            assert n.emit("mystery") is None
            assert len(q) == 0
        finally:
            n.unsubscribe(q)

    def test_dedup_collapses_second_event_for_same_slot(self) -> None:
        n = Narrator(dedup_window_sec=5.0)
        q = n.subscribe()
        try:
            now = time.time()
            first = n.emit(
                "take_failed",
                fields={"slot_id": "A1:1:0", "qa_axis": "x", "reason": "y"},
                timestamp=now,
            )
            second = n.emit(
                "take_failed",
                fields={"slot_id": "A1:1:0", "qa_axis": "x", "reason": "y"},
                timestamp=now + 0.1,
            )
            assert first is not None
            assert second is None
            assert len(q) == 1
        finally:
            n.unsubscribe(q)

    def test_dedup_does_not_collapse_distinct_slots(self) -> None:
        n = Narrator(dedup_window_sec=5.0)
        q = n.subscribe()
        try:
            now = time.time()
            a = n.emit(
                "take_failed",
                fields={"slot_id": "A1:1:0", "qa_axis": "x", "reason": "y"},
                timestamp=now,
            )
            b = n.emit(
                "take_failed",
                fields={"slot_id": "A1:2:0", "qa_axis": "x", "reason": "y"},
                timestamp=now + 0.1,
            )
            assert a is not None
            assert b is not None
            assert len(q) == 2
        finally:
            n.unsubscribe(q)

    def test_dedup_window_respects_timestamp_gap(self) -> None:
        n = Narrator(dedup_window_sec=1.0)
        q = n.subscribe()
        try:
            now = time.time()
            assert n.emit(
                "stage_started",
                fields={"stage": "audio"},
                timestamp=now,
            ) is not None
            # Outside window -> second event fires.
            assert n.emit(
                "stage_started",
                fields={"stage": "audio"},
                timestamp=now + 5,
            ) is not None
            assert len(q) == 2
        finally:
            n.unsubscribe(q)

    def test_emit_fans_out_to_all_subscribers(self) -> None:
        n = Narrator(dedup_window_sec=0.0)
        a = n.subscribe()
        b = n.subscribe()
        try:
            n.emit("stage_started", fields={"stage": "audio"})
            assert len(a) == 1 and len(b) == 1
            assert a[0]["text"] == b[0]["text"]
        finally:
            n.unsubscribe(a)
            n.unsubscribe(b)

    def test_payload_carries_id_and_rendered_text(self) -> None:
        n = Narrator(dedup_window_sec=0.0)
        q = n.subscribe()
        try:
            n.emit(
                "take_failed",
                fields={"slot_id": "A1:3:0", "qa_axis": "loudness", "reason": "low"},
            )
            payload = q[0]
            assert payload["id"]
            assert payload["kind"] == "take_failed"
            assert "[[slot:A1:3:0]]" in payload["text"]
        finally:
            n.unsubscribe(q)


# ---------------------------------------------------------------------------
# Pipeline safety — emission is non-blocking and bounded
# ---------------------------------------------------------------------------


def test_emit_never_blocks_on_full_subscriber() -> None:
    """A subscriber with no active reader must not block the pipeline.

    The narrator uses a bounded ``collections.deque`` with ``maxlen``; when
    it fills up the oldest events are dropped.  We simulate a wedged
    reader by flooding the queue with more events than its capacity and
    verify that ``emit`` always completes well under a timeout.
    """
    n = Narrator(dedup_window_sec=0.0)
    q = n.subscribe()
    try:
        deadline = time.monotonic() + 2.0
        for i in range(2_000):
            n.emit(
                "stage_started",
                fields={"stage": f"stage_{i}"},
            )
            assert time.monotonic() < deadline, (
                "emit() took longer than 2s for 2000 events -- pipeline "
                "safety invariant (non-blocking) is violated"
            )
        assert len(q) <= q.maxlen
    finally:
        n.unsubscribe(q)


def test_emit_is_threadsafe_under_contention() -> None:
    """Concurrent emits from many threads must not corrupt dedup state."""
    n = Narrator(dedup_window_sec=0.0)
    q = n.subscribe()
    try:
        errors: list[BaseException] = []

        def worker(slot: str, count: int) -> None:
            try:
                for i in range(count):
                    n.emit(
                        "take_failed",
                        fields={
                            "slot_id": f"{slot}:{i}",
                            "qa_axis": "x",
                            "reason": "y",
                        },
                    )
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"S{j}", 50)) for j in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors
        # 8 threads * 50 distinct slots = 400 unique events, all published.
        assert len(q) == 400
    finally:
        n.unsubscribe(q)


# ---------------------------------------------------------------------------
# Singleton facade — subscribe / unsubscribe
# ---------------------------------------------------------------------------


def test_singleton_subscribe_and_emit() -> None:
    q = subscribe_narrator_events()
    try:
        emit_narrator_event(
            "stage_completed",
            fields={"stage": "audio"},
        )
        assert len(q) == 1
        assert q[0]["text"] == "Audio complete."
    finally:
        unsubscribe_narrator_events(q)


def test_unsubscribe_stops_delivery() -> None:
    q = subscribe_narrator_events()
    unsubscribe_narrator_events(q)
    # Force past the dedup window by waiting the default interval.
    time.sleep(DEDUP_WINDOW_SEC + 0.05)
    emit_narrator_event("stage_started", fields={"stage": "scenario"})
    assert len(q) == 0


def test_get_narrator_returns_singleton() -> None:
    assert get_narrator() is get_narrator()


# ---------------------------------------------------------------------------
# Reasoning-digest bridge — map reasoning digests onto narrator events
# ---------------------------------------------------------------------------


class TestReasoningDigestBridge:
    def _digest(
        self,
        kind: str,
        source: dict,
        *,
        scope: str = "stage",
    ) -> dict:
        return {
            "timestamp": time.time(),
            "kind": kind,
            "scope": scope,
            "summary": "ignored by bridge",
            "source_event": source,
        }

    def test_stage_start_bridges_to_stage_started(self) -> None:
        q = subscribe_narrator_events()
        try:
            # Use a unique timestamp per test to bypass dedup across tests.
            time.sleep(DEDUP_WINDOW_SEC + 0.05)
            result = bridge_from_reasoning_digest(
                self._digest("stage_start", {"stage": "scenario"})
            )
            assert result is not None
            assert result.kind == "stage_started"
            assert q[-1]["text"] == "Starting scenario\u2026"
        finally:
            unsubscribe_narrator_events(q)

    def test_qa_verdict_fail_bridges_to_take_failed_with_slot(self) -> None:
        q = subscribe_narrator_events()
        try:
            time.sleep(DEDUP_WINDOW_SEC + 0.05)
            payload = self._digest(
                "qa_verdict",
                {
                    "verdict": "fail",
                    "scene_num": 4,
                    "phrase_idx": 2,
                    "artifact_type": "narration",
                    "check_name": "loudness",
                    "message": "below -23 LUFS",
                },
                scope="element",
            )
            result = bridge_from_reasoning_digest(payload)
            assert result is not None
            assert result.kind == "take_failed"
            text = q[-1]["text"]
            assert "[[slot:A1:4:2]]" in text
            assert "loudness" in text
            assert "below -23 LUFS" in text
        finally:
            unsubscribe_narrator_events(q)

    def test_qa_verdict_pass_is_not_promoted(self) -> None:
        q = subscribe_narrator_events()
        try:
            time.sleep(DEDUP_WINDOW_SEC + 0.05)
            payload = self._digest(
                "qa_verdict",
                {"verdict": "pass", "scene_num": 1, "phrase_idx": 0},
            )
            result = bridge_from_reasoning_digest(payload)
            assert result is None
        finally:
            unsubscribe_narrator_events(q)

    def test_internal_tag_suppresses_even_known_kind(self) -> None:
        q = subscribe_narrator_events()
        try:
            time.sleep(DEDUP_WINDOW_SEC + 0.05)
            before = len(q)
            payload = self._digest(
                "stage_start",
                {"stage": "scenario", "tags": ["internal"]},
            )
            result = bridge_from_reasoning_digest(payload)
            assert result is None
            assert len(q) == before
        finally:
            unsubscribe_narrator_events(q)

    def test_bridge_returns_none_for_non_promoted_kind(self) -> None:
        q = subscribe_narrator_events()
        try:
            time.sleep(DEDUP_WINDOW_SEC + 0.05)
            before = len(q)
            payload = self._digest(
                "eta_revision",
                {"stage": "audio", "old_eta": 10, "new_eta": 15},
            )
            result = bridge_from_reasoning_digest(payload)
            # eta_revision isn't in the narrator vocabulary and doesn't
            # opt in -> silent drop.
            assert result is None
            assert len(q) == before
        finally:
            unsubscribe_narrator_events(q)
