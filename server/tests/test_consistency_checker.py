"""
Unit tests for the Consistency Checker (ARCH-A5, issue #135).

Covers the invariants declared in
``server/callbacks/consistency_checker.py``:

1. **Drift detection** -- stage derived at an older ledger revision yields
   a :class:`LedgerDrift` carrying ``from_rev``, ``to_rev``, ``artifact_ids``
   and the exact ledger records appended in between.
2. **No drift, no signal** -- stage derived at the current revision emits
   nothing and leaves the blackboard untouched.
3. **Untagged stage (pre-B1) warns, does not fail.**
4. **Fail-loud invariants** -- missing ledger state, revision decrease,
   malformed derivation entries, malformed drift queue.
5. **Registration surface** -- after-agent callback, before-tool callback,
   and gate-poll entry point all delegate to ``check_consistency`` and do
   not short-circuit.
6. **Signal queue** -- drift signals accumulate in insertion order and
   round-trip through JSON storage unchanged.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Make server/ imports work when running `pytest` from the repo root.
_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from callbacks.consistency_checker import (  # noqa: E402
    LEDGER_DRIFT_SIGNALS_KEY,
    STAGE_DERIVATIONS_KEY,
    LedgerDrift,
    after_agent_consistency_check,
    before_tool_consistency_check,
    check_consistency,
    check_consistency_at_gate,
    pending_drift_signals,
    record_stage_derivation,
)
from callbacks.preference_ledger import (  # noqa: E402
    PREFERENCE_LEDGER_KEY,
    Origin,
    Polarity,
    Scope,
    Subject,
    append_preference,
    current_revision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _origin(event_id: str = "L4-001", reviewer: str = "alice") -> Origin:
    return Origin(
        l4_event_id=event_id,
        reviewer=reviewer,
        timestamp="2026-04-18T12:00:00Z",
    )


def _seed_ledger(state: dict, n: int = 1) -> None:
    """Append ``n`` preference records so the ledger is non-empty."""
    for i in range(n):
        append_preference(
            state,
            scope=Scope.SCENE,
            scope_ref=f"scene-{i + 1}",
            polarity=Polarity.PREFER,
            subject=Subject.TONE,
            content=f"record {i + 1}",
            origin=_origin(f"L4-{i + 1:03d}"),
        )


def _fresh_state() -> dict:
    """Return a state with an initialised (empty) ledger."""
    return {PREFERENCE_LEDGER_KEY: "[]"}


# ---------------------------------------------------------------------------
# Empty / up-to-date behaviour
# ---------------------------------------------------------------------------


def test_up_to_date_stage_emits_no_drift():
    state = _fresh_state()
    _seed_ledger(state, 3)
    record_stage_derivation(state, "scenario", artifact_ids=["scene-1"])

    drift = check_consistency(state, "scenario")

    assert drift is None
    assert pending_drift_signals(state) == []


def test_empty_ledger_up_to_date_stage_is_no_op():
    state = _fresh_state()
    record_stage_derivation(state, "scenario")  # revision=0
    assert current_revision(state) == 0

    assert check_consistency(state, "scenario") is None
    assert pending_drift_signals(state) == []


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


def test_drift_emits_signal_with_new_records():
    state = _fresh_state()
    _seed_ledger(state, 2)  # ledger at rev 2
    record_stage_derivation(
        state, "scenario", artifact_ids=["scene-1", "scene-2"]
    )
    # Ledger advances after the stage was derived.
    append_preference(
        state,
        scope=Scope.GLOBAL,
        polarity=Polarity.PREFER,
        subject=Subject.PACING,
        content="faster",
        origin=_origin("L4-003"),
    )
    append_preference(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-3",
        polarity=Polarity.AVOID,
        subject=Subject.TONE,
        content="no melodrama",
        origin=_origin("L4-004"),
    )

    drift = check_consistency(state, "scenario")

    assert drift is not None
    assert drift.stage_name == "scenario"
    assert drift.from_rev == 2
    assert drift.to_rev == 4
    assert drift.artifact_ids == ("scene-1", "scene-2")
    assert len(drift.new_records) == 2
    assert [r["revision"] for r in drift.new_records] == [3, 4]
    assert drift.new_records[0]["content"] == "faster"
    assert drift.new_records[1]["content"] == "no melodrama"


def test_drift_signal_appended_to_blackboard_as_json():
    state = _fresh_state()
    _seed_ledger(state, 1)
    record_stage_derivation(state, "visual_direction")
    _seed_ledger(state, 1)  # now at rev 2
    # second seed starts from revision 2 because ledger is append-only
    # with its own monotonic counter. We only care that it advanced.

    drift = check_consistency(state, "visual_direction")
    assert drift is not None

    raw = state[LEDGER_DRIFT_SIGNALS_KEY]
    assert isinstance(raw, str)
    decoded = json.loads(raw)
    assert isinstance(decoded, list) and len(decoded) == 1
    assert decoded[0]["stage_name"] == "visual_direction"
    assert decoded[0]["from_rev"] == drift.from_rev
    assert decoded[0]["to_rev"] == drift.to_rev


def test_multiple_drift_signals_accumulate_in_order():
    state = _fresh_state()
    _seed_ledger(state, 1)
    record_stage_derivation(state, "scenario")
    record_stage_derivation(state, "audio")
    _seed_ledger(state, 1)

    d1 = check_consistency(state, "scenario")
    d2 = check_consistency(state, "audio")

    pending = pending_drift_signals(state)
    assert [s.stage_name for s in pending] == ["scenario", "audio"]
    assert pending[0].to_rev == d1.to_rev == 2
    assert pending[1].to_rev == d2.to_rev == 2


def test_new_records_are_only_strictly_after_derivation():
    state = _fresh_state()
    _seed_ledger(state, 3)
    record_stage_derivation(state, "scenario")  # rev 3
    _seed_ledger(state, 2)  # revs 4, 5

    drift = check_consistency(state, "scenario")
    assert drift is not None
    assert [r["revision"] for r in drift.new_records] == [4, 5]
    # Older records must NOT leak into the signal.
    assert all(r["revision"] > 3 for r in drift.new_records)


# ---------------------------------------------------------------------------
# Untagged (pre-B1) stages
# ---------------------------------------------------------------------------


def test_untagged_stage_warns_and_returns_none(caplog):
    state = _fresh_state()
    _seed_ledger(state, 2)

    with caplog.at_level(logging.WARNING, logger="callbacks.consistency_checker"):
        drift = check_consistency(state, "unknown_stage")

    assert drift is None
    assert pending_drift_signals(state) == []
    assert any(
        "unknown_stage" in rec.message and "untagged" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Fail-loud invariants
# ---------------------------------------------------------------------------


def test_missing_ledger_state_fails_loud():
    state: dict = {}  # no PREFERENCE_LEDGER_KEY at all
    with pytest.raises(RuntimeError, match="preference_ledger"):
        check_consistency(state, "scenario")


def test_revision_decrease_fails_loud():
    state = _fresh_state()
    _seed_ledger(state, 1)
    # Pretend B1 tagged this stage at a revision ahead of the ledger --
    # impossible if the ledger is append-only.
    state[STAGE_DERIVATIONS_KEY] = json.dumps(
        {"scenario": {"revision": 99, "artifact_ids": []}}
    )

    with pytest.raises(RuntimeError, match="append-only"):
        check_consistency(state, "scenario")


def test_malformed_derivation_entry_fails_loud():
    state = _fresh_state()
    state[STAGE_DERIVATIONS_KEY] = json.dumps({"scenario": "not-a-mapping"})
    with pytest.raises(TypeError, match="must be a mapping"):
        check_consistency(state, "scenario")


def test_missing_revision_field_fails_loud():
    state = _fresh_state()
    state[STAGE_DERIVATIONS_KEY] = json.dumps({"scenario": {"artifact_ids": []}})
    with pytest.raises(ValueError, match="missing 'revision'"):
        check_consistency(state, "scenario")


def test_non_integer_revision_fails_loud():
    state = _fresh_state()
    state[STAGE_DERIVATIONS_KEY] = json.dumps(
        {"scenario": {"revision": "3", "artifact_ids": []}}
    )
    with pytest.raises(TypeError, match="revision must be int"):
        check_consistency(state, "scenario")


def test_malformed_stage_derivations_blob_fails_loud():
    state = _fresh_state()
    state[STAGE_DERIVATIONS_KEY] = "{not json"
    with pytest.raises(ValueError, match="not valid JSON"):
        check_consistency(state, "scenario")


def test_stage_derivations_wrong_top_type_fails_loud():
    state = _fresh_state()
    state[STAGE_DERIVATIONS_KEY] = json.dumps(["nope"])
    with pytest.raises(ValueError, match="must decode to a dict"):
        check_consistency(state, "scenario")


def test_artifact_ids_bare_string_fails_loud():
    state = _fresh_state()
    state[STAGE_DERIVATIONS_KEY] = json.dumps(
        {"scenario": {"revision": 0, "artifact_ids": "scene-1"}}
    )
    with pytest.raises(TypeError, match="artifact_ids must be a list/tuple"):
        check_consistency(state, "scenario")


def test_empty_stage_name_rejected():
    state = _fresh_state()
    with pytest.raises(ValueError, match="stage_name"):
        check_consistency(state, "")


def test_malformed_drift_queue_fails_loud():
    state = _fresh_state()
    _seed_ledger(state, 1)
    record_stage_derivation(state, "scenario")
    _seed_ledger(state, 1)
    # Poison the signal queue with a non-list JSON value.
    state[LEDGER_DRIFT_SIGNALS_KEY] = json.dumps({"not": "a list"})

    with pytest.raises(ValueError, match="must decode to a list"):
        check_consistency(state, "scenario")


# ---------------------------------------------------------------------------
# record_stage_derivation helper
# ---------------------------------------------------------------------------


def test_record_stage_derivation_defaults_to_current_revision():
    state = _fresh_state()
    _seed_ledger(state, 4)

    record_stage_derivation(state, "scenario")

    stored = json.loads(state[STAGE_DERIVATIONS_KEY])
    assert stored["scenario"]["revision"] == 4
    assert stored["scenario"]["artifact_ids"] == []


def test_record_stage_derivation_preserves_existing_artifact_ids():
    state = _fresh_state()
    record_stage_derivation(state, "scenario", artifact_ids=["a", "b"])
    _seed_ledger(state, 2)

    record_stage_derivation(state, "scenario")  # no artifact_ids override

    stored = json.loads(state[STAGE_DERIVATIONS_KEY])
    assert stored["scenario"]["revision"] == 2
    assert stored["scenario"]["artifact_ids"] == ["a", "b"]


def test_record_stage_derivation_rejects_bare_string_ids():
    state = _fresh_state()
    with pytest.raises(TypeError, match="sequence of strings"):
        record_stage_derivation(state, "scenario", artifact_ids="scene-1")


def test_record_stage_derivation_rejects_non_string_ids():
    state = _fresh_state()
    with pytest.raises(TypeError, match="must be str"):
        record_stage_derivation(state, "scenario", artifact_ids=["ok", 42])


def test_record_stage_derivation_rejects_empty_stage_name():
    state = _fresh_state()
    with pytest.raises(ValueError, match="stage_name"):
        record_stage_derivation(state, "")


def test_record_stage_derivation_rejects_negative_revision():
    state = _fresh_state()
    with pytest.raises(ValueError, match=">= 0"):
        record_stage_derivation(state, "scenario", revision=-1)


# ---------------------------------------------------------------------------
# LedgerDrift record invariants
# ---------------------------------------------------------------------------


def test_ledger_drift_requires_forward_revisions():
    with pytest.raises(ValueError, match="to_rev"):
        LedgerDrift(
            stage_name="scenario",
            artifact_ids=(),
            from_rev=3,
            to_rev=3,
            new_records=(),
        )
    with pytest.raises(ValueError, match="to_rev"):
        LedgerDrift(
            stage_name="scenario",
            artifact_ids=(),
            from_rev=5,
            to_rev=2,
            new_records=(),
        )


def test_ledger_drift_rejects_empty_stage_name():
    with pytest.raises(ValueError, match="stage_name"):
        LedgerDrift(
            stage_name="",
            artifact_ids=(),
            from_rev=0,
            to_rev=1,
            new_records=(),
        )


def test_ledger_drift_rejects_non_int_revisions():
    with pytest.raises(TypeError, match="from_rev"):
        LedgerDrift(
            stage_name="scenario",
            artifact_ids=(),
            from_rev="0",  # type: ignore[arg-type]
            to_rev=1,
            new_records=(),
        )


def test_ledger_drift_is_frozen():
    drift = LedgerDrift(
        stage_name="scenario",
        artifact_ids=("a",),
        from_rev=0,
        to_rev=1,
        new_records=(),
    )
    with pytest.raises(Exception):
        drift.stage_name = "other"  # type: ignore[misc]


def test_ledger_drift_round_trips_through_pending_signals():
    state = _fresh_state()
    _seed_ledger(state, 1)
    record_stage_derivation(state, "scenario", artifact_ids=["a", "b"])
    _seed_ledger(state, 2)

    original = check_consistency(state, "scenario")
    assert original is not None

    (recovered,) = pending_drift_signals(state)
    assert recovered.stage_name == original.stage_name
    assert recovered.artifact_ids == original.artifact_ids
    assert recovered.from_rev == original.from_rev
    assert recovered.to_rev == original.to_rev
    assert [r["revision"] for r in recovered.new_records] == [
        r["revision"] for r in original.new_records
    ]


# ---------------------------------------------------------------------------
# Registration surface (callbacks + gate poll)
# ---------------------------------------------------------------------------


def test_after_agent_callback_uses_agent_name_and_emits_drift():
    state = _fresh_state()
    _seed_ledger(state, 1)
    record_stage_derivation(state, "scenario_director")
    _seed_ledger(state, 1)

    ctx = SimpleNamespace(state=state, agent_name="scenario_director")
    result = after_agent_consistency_check(ctx)

    assert result is None  # callback must not short-circuit
    pending = pending_drift_signals(state)
    assert len(pending) == 1
    assert pending[0].stage_name == "scenario_director"


def test_after_agent_callback_falls_back_to_pipeline_phase():
    state = _fresh_state()
    state["pipeline_phase"] = "audio"
    _seed_ledger(state, 1)
    record_stage_derivation(state, "audio")
    _seed_ledger(state, 1)

    ctx = SimpleNamespace(state=state)  # no agent_name attribute
    after_agent_consistency_check(ctx)

    (drift,) = pending_drift_signals(state)
    assert drift.stage_name == "audio"


def test_before_tool_callback_delegates_and_returns_none():
    state = _fresh_state()
    _seed_ledger(state, 1)
    record_stage_derivation(state, "production")
    _seed_ledger(state, 1)

    tool = SimpleNamespace(name="generate_video_clip")
    tool_ctx = SimpleNamespace(state=state, agent_name="production")

    result = before_tool_consistency_check(tool, {"prompt": "x"}, tool_ctx)

    assert result is None  # tool call must proceed
    (drift,) = pending_drift_signals(state)
    assert drift.stage_name == "production"


def test_before_tool_callback_does_not_short_circuit_on_no_drift():
    state = _fresh_state()
    _seed_ledger(state, 2)
    record_stage_derivation(state, "production")

    tool = SimpleNamespace(name="probe_clip")
    tool_ctx = SimpleNamespace(state=state, agent_name="production")
    assert (
        before_tool_consistency_check(tool, {}, tool_ctx)
        is None
    )
    assert pending_drift_signals(state) == []


def test_gate_entry_point_is_equivalent_to_check_consistency():
    state = _fresh_state()
    _seed_ledger(state, 1)
    record_stage_derivation(state, "scenario")
    _seed_ledger(state, 1)

    drift = check_consistency_at_gate(state, "scenario")

    assert drift is not None
    assert drift.stage_name == "scenario"
    assert pending_drift_signals(state) == [drift]


def test_callbacks_fail_loud_when_ledger_missing():
    """Callbacks must not swallow the missing-ledger invariant violation."""
    state: dict = {}  # no PREFERENCE_LEDGER_KEY

    ctx = SimpleNamespace(state=state, agent_name="scenario")
    with pytest.raises(RuntimeError):
        after_agent_consistency_check(ctx)

    tool = SimpleNamespace(name="noop")
    tool_ctx = SimpleNamespace(state=state, agent_name="scenario")
    with pytest.raises(RuntimeError):
        before_tool_consistency_check(tool, {}, tool_ctx)

    with pytest.raises(RuntimeError):
        check_consistency_at_gate(state, "scenario")


# ---------------------------------------------------------------------------
# A5 does not trigger re-manifestation here (explicit scope boundary)
# ---------------------------------------------------------------------------


def test_a5_never_mutates_ledger_or_stage_derivation():
    """Detection-only: check_consistency must not append to the ledger,
    mutate existing records, or clear the derivation tag.

    Re-manifestation is ARCH-A6; this module only signals.
    """
    state = _fresh_state()
    _seed_ledger(state, 1)
    record_stage_derivation(state, "scenario", artifact_ids=["a"])
    _seed_ledger(state, 2)

    ledger_before = state[PREFERENCE_LEDGER_KEY]
    deriv_before = state[STAGE_DERIVATIONS_KEY]

    check_consistency(state, "scenario")

    assert state[PREFERENCE_LEDGER_KEY] == ledger_before
    assert state[STAGE_DERIVATIONS_KEY] == deriv_before
