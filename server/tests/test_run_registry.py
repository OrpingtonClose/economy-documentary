"""UI-07 — unit tests for the per-run event ring buffer.

Covers the behavioural contract declared in :mod:`server.run_registry`:

1. Fresh runs mint a unique ``run-<hex>`` id and become the "latest".
2. ``append`` allocates a monotonic seq id and stores the exact SSE text
   so replay output is byte-for-byte identical to live output.
3. Replay within the buffer returns only events with seq > last_id.
4. Replay across the buffer boundary (ring eviction) reports overflow.
5. ``replay(run_id, 0)`` is a "fresh tab" handshake and never overflows.
6. Unknown run ids are inert (no exception, returns empty).
7. Simultaneous reconnects from two virtual clients each get a full
   consistent replay — i.e. replay is idempotent w.r.t. the buffer.
8. Memory stays bounded: a tiny maxlen caps deque length to maxlen.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from run_registry import RunRegistry  # noqa: E402


# ---------------------------------------------------------------------------
# Fresh-run / id semantics
# ---------------------------------------------------------------------------


def test_create_mints_unique_prefixed_id():
    r = RunRegistry(maxlen=8)
    a = r.create()
    b = r.create()
    assert a.startswith("run-") and len(a) > len("run-")
    assert b.startswith("run-")
    assert a != b
    # Latest is the most-recently created.
    assert r.latest() == b
    assert r.exists(a) and r.exists(b)


def test_create_with_explicit_id_is_idempotent():
    r = RunRegistry(maxlen=8)
    a = r.create("run-explicit")
    b = r.create("run-explicit")
    assert a == b == "run-explicit"
    assert r.latest() == "run-explicit"


# ---------------------------------------------------------------------------
# Append + replay within the buffer
# ---------------------------------------------------------------------------


def test_append_assigns_monotonic_seq():
    r = RunRegistry(maxlen=8)
    rid = r.create()
    assert r.append(rid, "data: a\n\n") == 1
    assert r.append(rid, "data: b\n\n") == 2
    assert r.append(rid, "data: c\n\n") == 3
    assert r.latest_seq(rid) == 3


def test_replay_returns_only_events_after_last_id():
    r = RunRegistry(maxlen=8)
    rid = r.create()
    for i in range(5):
        r.append(rid, f"data: {i}\n\n")
    events, overflow, latest = r.replay(rid, last_event_id=2)
    assert [seq for seq, _ in events] == [3, 4, 5]
    assert overflow is False
    assert latest == 5


def test_replay_from_zero_returns_full_buffer():
    r = RunRegistry(maxlen=8)
    rid = r.create()
    for _ in range(3):
        r.append(rid, "data: x\n\n")
    events, overflow, latest = r.replay(rid, last_event_id=0)
    assert [seq for seq, _ in events] == [1, 2, 3]
    assert overflow is False
    assert latest == 3


def test_replay_preserves_sse_text_byte_for_byte():
    r = RunRegistry(maxlen=4)
    rid = r.create()
    raw = 'data: {"type":"pipeline_event","value":42}\n\n'
    seq = r.append(rid, raw)
    events, _, _ = r.replay(rid, last_event_id=seq - 1)
    assert events == [(seq, raw)]


# ---------------------------------------------------------------------------
# Ring eviction + buffer_overflow semantics (issue #212 core invariant)
# ---------------------------------------------------------------------------


def test_ring_buffer_evicts_oldest_when_full():
    r = RunRegistry(maxlen=3)
    rid = r.create()
    for i in range(5):
        r.append(rid, f"data: {i}\n\n")
    assert r.buffer_size(rid) == 3
    # Evicted seq==1 and seq==2; buffer now holds 3/4/5.
    events, _, _ = r.replay(rid, last_event_id=0)
    assert [seq for seq, _ in events] == [3, 4, 5]


def test_replay_across_buffer_boundary_reports_overflow():
    r = RunRegistry(maxlen=3)
    rid = r.create()
    for i in range(5):
        r.append(rid, f"data: {i}\n\n")
    # Client last saw seq=1 but the buffer now starts at seq=3 -- seq=2
    # was evicted, so the client has a gap.
    events, overflow, latest = r.replay(rid, last_event_id=1)
    assert overflow is True
    assert latest == 5
    # Events with seq > last_id are still returned so the UI can keep
    # applying reducers for anything it *did* have time to buffer.
    assert [seq for seq, _ in events] == [3, 4, 5]


def test_replay_at_boundary_exact_no_overflow():
    """Client last-id matches the oldest buffered event → contiguous."""
    r = RunRegistry(maxlen=3)
    rid = r.create()
    for i in range(5):
        r.append(rid, f"data: {i}\n\n")
    # Buffer holds [3,4,5]. Client last=2 → gap-free (next is 3).
    events, overflow, _ = r.replay(rid, last_event_id=2)
    assert overflow is False
    assert [seq for seq, _ in events] == [3, 4, 5]


def test_fresh_tab_never_overflows_even_after_eviction():
    r = RunRegistry(maxlen=2)
    rid = r.create()
    for i in range(10):
        r.append(rid, f"data: {i}\n\n")
    events, overflow, _ = r.replay(rid, last_event_id=0)
    assert overflow is False
    assert [seq for seq, _ in events] == [9, 10]


# ---------------------------------------------------------------------------
# Unknown run ids are inert
# ---------------------------------------------------------------------------


def test_append_to_unknown_run_is_noop():
    r = RunRegistry(maxlen=8)
    assert r.append("run-does-not-exist", "data: x\n\n") == 0


def test_replay_of_unknown_run_returns_empty():
    r = RunRegistry(maxlen=8)
    events, overflow, latest = r.replay("run-unknown", last_event_id=0)
    assert events == []
    assert overflow is False
    assert latest == 0


def test_exists_returns_false_for_unknown_and_true_for_known():
    r = RunRegistry(maxlen=8)
    rid = r.create()
    assert r.exists(rid)
    assert not r.exists("run-nope")


# ---------------------------------------------------------------------------
# Simultaneous reconnects
# ---------------------------------------------------------------------------


def test_two_clients_reconnecting_see_identical_replay():
    r = RunRegistry(maxlen=100)
    rid = r.create()
    for i in range(10):
        r.append(rid, f"data: {i}\n\n")
    tab_a_events, a_overflow, _ = r.replay(rid, last_event_id=3)
    tab_b_events, b_overflow, _ = r.replay(rid, last_event_id=3)
    assert tab_a_events == tab_b_events
    assert a_overflow == b_overflow is False


def test_replay_is_idempotent_when_called_twice():
    r = RunRegistry(maxlen=100)
    rid = r.create()
    for i in range(5):
        r.append(rid, f"data: {i}\n\n")
    first = r.replay(rid, 2)
    second = r.replay(rid, 2)
    assert first == second


# ---------------------------------------------------------------------------
# Bounded memory
# ---------------------------------------------------------------------------


def test_memory_is_bounded_by_maxlen():
    r = RunRegistry(maxlen=50)
    rid = r.create()
    for i in range(500):
        r.append(rid, f"data: {i}\n\n")
    assert r.buffer_size(rid) == 50
    # Seq counter keeps climbing even though the ring wraps.
    assert r.latest_seq(rid) == 500


# ---------------------------------------------------------------------------
# Last-event-id parsing robustness (ensures replay handles bogus inputs).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [-1, -999])
def test_replay_treats_negative_last_id_as_zero(bad: int):
    r = RunRegistry(maxlen=8)
    rid = r.create()
    for i in range(3):
        r.append(rid, f"data: {i}\n\n")
    events, overflow, _ = r.replay(rid, last_event_id=bad)
    # Negative behaves like "fresh tab" — full replay, no overflow.
    assert overflow is False
    assert [seq for seq, _ in events] == [1, 2, 3]
