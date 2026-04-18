"""
Unit tests for ARCH-B3 re-manifestation executor (issue #139).

Covers the invariants declared in ``server/callbacks/remanifestation.py``:

1. **Plan protocol validation** -- malformed plans raise loudly.
2. **Queue round-trip** -- ``enqueue_plan`` + ``drain_plans`` preserves
   order and plan contents through JSON.
3. **``execute_plan`` clears artifact tags and stage derivations** named
   by the plan so the re-running stage can re-tag at the current ledger
   revision without hitting ``ArtifactAlreadyTaggedError``.
4. **History log** -- every execution / escalation / failure appends a
   structured entry to ``remanifestation_history``.
5. **Fail-loud executor** -- missing ledger state raises
   :class:`RemanifestationError`; a raising runner bubbles up wrapped.
6. **Drift-signal dispatch** -- ``handle_drift_signals`` drains the A5
   queue, turns each drift into a plan (or escalates on ``None``), runs
   the plan, and re-escalates to human L4 on plan failure.
7. **Plan provider protocol is runtime-checkable** -- custom providers
   (tests simulate A6) plug in without importing B3 internals.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from callbacks.artifact_revision_tag import (  # noqa: E402
    has_tag,
    tag_artifact,
)
from callbacks.consistency_checker import (  # noqa: E402
    STAGE_DERIVATIONS_KEY,
    LedgerDrift,
    check_consistency,
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
)
from callbacks.remanifestation import (  # noqa: E402
    REMANIFESTATION_PLAN_QUEUE_KEY,
    DefaultPlanProvider,
    DictRemanifestationPlan,
    RemanifestationError,
    RemanifestationPlan,
    drain_plans,
    enqueue_plan,
    execute_plan,
    handle_drift_signals,
    list_pending_plans,
    remanifestation_history,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _origin(event_id: str = "L4-001") -> Origin:
    return Origin(
        l4_event_id=event_id,
        reviewer="tester",
        timestamp="2026-04-18T12:00:00Z",
    )


def _seed_ledger(state: dict, n: int = 1, start: int = 1) -> None:
    for i in range(n):
        append_preference(
            state,
            scope=Scope.SCENE,
            scope_ref=f"scene-{start + i}",
            polarity=Polarity.PREFER,
            subject=Subject.TONE,
            content=f"record {start + i}",
            origin=_origin(f"L4-{start + i:03d}"),
        )


def _fresh_state() -> dict:
    return {PREFERENCE_LEDGER_KEY: "[]"}


def _tagged_state() -> dict:
    """State with ledger seeded, one stage derived against an old rev, and a
    tagged artifact on that stage so a real drift can be generated."""
    state = _fresh_state()
    _seed_ledger(state, 1)  # rev 1
    state["scenario_scenes"] = [{"id": "scene-1"}]  # artifact payload
    tag_artifact(state, "scenario_scenes", stage="scenario")
    record_stage_derivation(
        state, "scenario", revision=1, artifact_ids=["scenario_scenes"]
    )
    _seed_ledger(state, 2, start=2)  # revs 2, 3 -- now stale
    return state


def _drift_for(state: dict, stage: str = "scenario") -> LedgerDrift:
    drift = check_consistency(state, stage)
    assert drift is not None, "test helper expected drift to be detectable"
    return drift


# ---------------------------------------------------------------------------
# Plan protocol validation
# ---------------------------------------------------------------------------


def test_dict_plan_rejects_empty_stages():
    with pytest.raises(ValueError, match="stages_to_rerun"):
        DictRemanifestationPlan(
            plan_id="p1",
            triggered_by={},
            stages_to_rerun=(),
            artifact_keys_to_clear=(),
            rationale="r",
        )


def test_dict_plan_rejects_non_string_stage():
    with pytest.raises(ValueError, match="stages_to_rerun"):
        DictRemanifestationPlan(
            plan_id="p1",
            triggered_by={},
            stages_to_rerun=("",),  # empty string is not a stage
            artifact_keys_to_clear=(),
            rationale="r",
        )


def test_dict_plan_rejects_non_string_plan_id():
    with pytest.raises(ValueError, match="plan_id"):
        DictRemanifestationPlan(
            plan_id="",
            triggered_by={},
            stages_to_rerun=("scenario",),
            artifact_keys_to_clear=(),
            rationale="r",
        )


def test_dict_plan_round_trips_through_dict():
    plan = DictRemanifestationPlan(
        plan_id="p1",
        triggered_by={"stage_name": "scenario", "from_rev": 1, "to_rev": 2},
        stages_to_rerun=("scenario",),
        artifact_keys_to_clear=("scenario_scenes",),
        rationale="preference changed",
    )
    restored = DictRemanifestationPlan.from_dict(plan.to_dict())
    assert restored == plan


def test_dict_plan_from_dict_rejects_missing_fields():
    with pytest.raises(ValueError, match="missing required fields"):
        DictRemanifestationPlan.from_dict(
            {
                "plan_id": "p1",
                # triggered_by missing
                "stages_to_rerun": ["scenario"],
                "artifact_keys_to_clear": [],
                "rationale": "r",
            }
        )


def test_runtime_checkable_protocol_accepts_dict_plan():
    plan = DictRemanifestationPlan(
        plan_id="p1",
        triggered_by={},
        stages_to_rerun=("scenario",),
        artifact_keys_to_clear=(),
        rationale="r",
    )
    assert isinstance(plan, RemanifestationPlan)


# ---------------------------------------------------------------------------
# Queue round-trip
# ---------------------------------------------------------------------------


def test_enqueue_and_drain_round_trip_preserves_order():
    state = _fresh_state()
    p1 = DictRemanifestationPlan(
        plan_id="p1",
        triggered_by={},
        stages_to_rerun=("scenario",),
        artifact_keys_to_clear=(),
        rationale="first",
    )
    p2 = DictRemanifestationPlan(
        plan_id="p2",
        triggered_by={},
        stages_to_rerun=("audio",),
        artifact_keys_to_clear=(),
        rationale="second",
    )
    enqueue_plan(state, p1)
    enqueue_plan(state, p2)

    assert [p.plan_id for p in list_pending_plans(state)] == ["p1", "p2"]

    drained = drain_plans(state)
    assert [p.plan_id for p in drained] == ["p1", "p2"]
    assert list_pending_plans(state) == []
    # Queue is reset to empty list, not missing (preserve JSON type).
    assert json.loads(state[REMANIFESTATION_PLAN_QUEUE_KEY]) == []


def test_enqueue_requires_plan_compatible_object():
    state = _fresh_state()
    with pytest.raises(TypeError, match="RemanifestationPlan"):
        enqueue_plan(state, object())


# ---------------------------------------------------------------------------
# execute_plan -- side effects
# ---------------------------------------------------------------------------


def test_execute_plan_clears_artifact_tags_and_stage_derivations():
    state = _tagged_state()
    # Pre-condition: tag + derivation both present.
    assert has_tag(state, "scenario_scenes")
    assert "scenario" in json.loads(state[STAGE_DERIVATIONS_KEY])

    plan = DictRemanifestationPlan(
        plan_id="p-test",
        triggered_by={"stage_name": "scenario"},
        stages_to_rerun=("scenario",),
        artifact_keys_to_clear=("scenario_scenes",),
        rationale="test clearing",
    )

    execute_plan(state, plan, gate=False)

    assert not has_tag(state, "scenario_scenes")
    assert "scenario" not in json.loads(state[STAGE_DERIVATIONS_KEY])


def test_execute_plan_invokes_runner_per_stage_in_order():
    state = _tagged_state()
    plan = DictRemanifestationPlan(
        plan_id="p-multi",
        triggered_by={},
        stages_to_rerun=("scenario", "audio", "visual"),
        artifact_keys_to_clear=(),
        rationale="multi",
    )
    seen: list[str] = []

    def runner(_state, stage):
        seen.append(stage)

    execute_plan(state, plan, runner=runner, gate=False)

    assert seen == ["scenario", "audio", "visual"]


def test_execute_plan_records_history_on_success():
    state = _tagged_state()
    plan = DictRemanifestationPlan(
        plan_id="p-hist",
        triggered_by={},
        stages_to_rerun=("scenario",),
        artifact_keys_to_clear=("scenario_scenes",),
        rationale="history-test",
    )

    execute_plan(state, plan, gate=False)

    hist = remanifestation_history(state)
    assert len(hist) == 1
    assert hist[0]["plan_id"] == "p-hist"
    assert hist[0]["status"] == "executed"
    assert "scenario" in hist[0]["note"] or "stage" in hist[0]["note"]


def test_execute_plan_fails_loud_on_missing_ledger():
    state: dict = {}  # no ledger -- invariant violation
    plan = DictRemanifestationPlan(
        plan_id="p1",
        triggered_by={},
        stages_to_rerun=("scenario",),
        artifact_keys_to_clear=(),
        rationale="r",
    )
    with pytest.raises(RemanifestationError, match="preference_ledger"):
        execute_plan(state, plan, gate=False)


def test_execute_plan_wraps_runner_exceptions_as_remanifestation_error():
    state = _tagged_state()
    plan = DictRemanifestationPlan(
        plan_id="p-fail",
        triggered_by={},
        stages_to_rerun=("scenario",),
        artifact_keys_to_clear=(),
        rationale="fail-test",
    )

    def bad_runner(_state, _stage):
        raise RuntimeError("runner boom")

    with pytest.raises(RemanifestationError, match="runner boom"):
        execute_plan(state, plan, runner=bad_runner, gate=False)

    # A failure history entry should have been appended.
    hist = remanifestation_history(state)
    assert hist[-1]["status"] == "failed"
    assert hist[-1]["plan_id"] == "p-fail"


def test_execute_plan_skips_untagged_artifacts_quietly():
    """Clearing an artifact that was never tagged must not raise."""
    state = _tagged_state()
    # Extra unknown key in plan -- executor must tolerate it.
    plan = DictRemanifestationPlan(
        plan_id="p-mix",
        triggered_by={},
        stages_to_rerun=("scenario",),
        artifact_keys_to_clear=("scenario_scenes", "never_tagged"),
        rationale="r",
    )
    execute_plan(state, plan, gate=False)  # must not raise

    assert not has_tag(state, "scenario_scenes")
    assert not has_tag(state, "never_tagged")


# ---------------------------------------------------------------------------
# Drift-signal dispatch (B2 <-> B3 bridge)
# ---------------------------------------------------------------------------


class _RecordingEscalator:
    """Drop-in escalator stub that records calls and returns fake ids."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, state, drift, plan, reason):
        self.calls.append(
            {
                "stage": drift.stage_name,
                "plan_id": plan.plan_id if plan is not None else None,
                "reason": reason,
            }
        )
        return f"ESC-{len(self.calls)}"


def test_handle_drift_signals_executes_default_plan_and_drains_queue():
    state = _tagged_state()
    _drift_for(state)  # enqueue a drift signal

    results = handle_drift_signals(state, gate=False)

    assert len(results) == 1
    assert results[0]["outcome"] == "executed"
    assert results[0]["stage_name"] == "scenario"
    assert results[0]["plan_id"] is not None
    # Drift queue drained.
    assert pending_drift_signals(state) == []
    # History recorded an execution.
    hist = remanifestation_history(state)
    assert any(h["status"] == "executed" for h in hist)


def test_handle_drift_signals_escalates_when_provider_returns_none():
    state = _tagged_state()
    _drift_for(state)
    escalator = _RecordingEscalator()

    class NoPlanProvider:
        def plan_for_drift(self, state, drift):
            return None

    results = handle_drift_signals(
        state,
        plan_provider=NoPlanProvider(),
        escalator=escalator,
        gate=False,
    )

    assert len(results) == 1
    assert results[0]["outcome"] == "escalated"
    assert results[0]["plan_id"] is None
    assert escalator.calls and escalator.calls[0]["stage"] == "scenario"
    assert "no plan" in escalator.calls[0]["reason"].lower()
    # Drift queue still drained even on escalation.
    assert pending_drift_signals(state) == []


def test_handle_drift_signals_re_escalates_on_plan_execution_failure():
    state = _tagged_state()
    _drift_for(state)
    escalator = _RecordingEscalator()

    def bad_runner(_state, _stage):
        raise RuntimeError("stage exploded")

    results = handle_drift_signals(
        state,
        runner=bad_runner,
        escalator=escalator,
        gate=False,
    )

    assert len(results) == 1
    assert results[0]["outcome"] == "escalated"
    assert results[0]["plan_id"] is not None  # plan existed but failed
    assert escalator.calls
    assert "execution failed" in escalator.calls[0]["reason"]
    # History shows both the failed execution AND the escalation.
    statuses = [h["status"] for h in remanifestation_history(state)]
    assert "failed" in statuses
    assert "escalated" in statuses


def test_handle_drift_signals_is_idempotent_when_queue_empty():
    state = _fresh_state()
    results = handle_drift_signals(state, gate=False)
    assert results == []
    assert remanifestation_history(state) == []


def test_default_plan_provider_skips_drift_with_no_tagged_artifacts():
    """Stage-derivation tagged without a corresponding artifact tag --
    default provider must return ``None`` so B3 escalates instead of
    silently succeeding on nothing."""
    state = _fresh_state()
    _seed_ledger(state, 1)
    record_stage_derivation(
        state, "scenario", revision=1, artifact_ids=["phantom-key"]
    )
    _seed_ledger(state, 1, start=2)
    drift = _drift_for(state)

    assert DefaultPlanProvider().plan_for_drift(state, drift) is None


def test_custom_plan_provider_plugs_in_without_b3_imports():
    """An A6-like provider that implements the protocol inline must
    compose with ``handle_drift_signals`` -- B3 never imports A6."""
    state = _tagged_state()
    _drift_for(state)

    class StubA6:
        def plan_for_drift(self, state, drift):
            return DictRemanifestationPlan(
                plan_id=f"stub-{drift.stage_name}",
                triggered_by=drift.to_dict(),
                stages_to_rerun=(drift.stage_name, "downstream"),
                artifact_keys_to_clear=("scenario_scenes",),
                rationale="stub A6 impact analysis",
            )

    seen: list[str] = []

    def runner(_state, stage):
        seen.append(stage)

    results = handle_drift_signals(
        state,
        plan_provider=StubA6(),
        runner=runner,
        gate=False,
    )

    assert len(results) == 1
    assert results[0]["outcome"] == "executed"
    assert results[0]["plan_id"] == "stub-scenario"
    assert seen == ["scenario", "downstream"]
