"""
Unit tests for ARCH-A6 re-manifestation pipeline (#136).

Covers the invariants declared in
:mod:`server.callbacks.remanifestation`:

1. **Impact analysis** -- GLOBAL records invalidate every tagged
   artifact regardless of stage; STAGE records match only their
   stage; narrower-scope records substring-match the artifact key.
2. **Empty plans** -- drift with no impacted artifacts produces an
   empty :class:`RemanifestationPlan`, not a spurious step.
3. **Action selection** -- hard (FORBID/REQUIRE) records on
   scene-scoped artifacts escalate to ``rewrite_scene``; DURATION
   PREFER/REQUIRE yields ``generate_extension_clip``; everything
   else is ``regenerate_clip``.
4. **Validator** -- rejects forbidden (``trim_narration`` et al.)
   actions, rejects plans that reference untagged artifacts, and
   rejects plans that contradict current-ledger FORBID records.
5. **Executor** -- queues each step via the default executor, appends
   receipts, clears the artifact tag after successful dispatch, and
   is idempotent on a second pass.
6. **Top-level orchestration** -- :func:`handle_drift` consumes every
   queued signal in FIFO order, re-enqueues only the signals whose
   plans failed validation, and leaves a successful run with an empty
   drift queue.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from callbacks.artifact_revision_tag import (  # noqa: E402
    ARTIFACT_REVISION_TAGS_KEY,
    has_tag,
    list_tags,
    tag_artifact,
)
from callbacks.consistency_checker import (  # noqa: E402
    LEDGER_DRIFT_SIGNALS_KEY,
    LedgerDrift,
    check_consistency,
    pending_drift_signals,
    record_stage_derivation,
)
from callbacks.preference_ledger import (  # noqa: E402
    PREFERENCE_LEDGER_KEY,
    Origin,
    Polarity,
    PreferenceRecord,
    Scope,
    Subject,
    append_preference,
    current_revision,
)
from callbacks.remanifestation import (  # noqa: E402
    ALLOWED_ACTIONS,
    InvalidPlanError,
    PlanStep,
    REMANIFESTATION_QUEUE_KEY,
    REMANIFESTATION_RECEIPTS_KEY,
    RemanifestationPlan,
    analyse_impact,
    execute_plan,
    handle_drift,
    plan_remanifestation,
    validate_plan,
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


def _fresh_state() -> dict:
    return {PREFERENCE_LEDGER_KEY: "[]"}


def _append(state: dict, **kwargs) -> PreferenceRecord:
    kwargs.setdefault("scope", Scope.GLOBAL)
    kwargs.setdefault("scope_ref", None)
    kwargs.setdefault("polarity", Polarity.PREFER)
    kwargs.setdefault("subject", Subject.TONE)
    kwargs.setdefault("content", "placeholder")
    kwargs.setdefault("origin", _origin())
    if kwargs["scope"] is Scope.GLOBAL:
        kwargs["scope_ref"] = None
    return append_preference(state, **kwargs)


def _tag(state: dict, artifact_key: str, stage: str) -> None:
    tag_artifact(state, artifact_key=artifact_key, stage=stage)


def _make_drift(
    state: dict,
    *,
    stage_name: str,
    from_rev: int,
    new_records: list[PreferenceRecord],
    artifact_ids: tuple[str, ...] = (),
) -> LedgerDrift:
    to_rev = current_revision(state)
    return LedgerDrift(
        stage_name=stage_name,
        artifact_ids=artifact_ids,
        from_rev=from_rev,
        to_rev=to_rev,
        new_records=tuple(r.to_dict() for r in new_records),
    )


# ---------------------------------------------------------------------------
# analyse_impact
# ---------------------------------------------------------------------------


def test_global_record_invalidates_every_tagged_artifact():
    state = _fresh_state()
    _tag(state, "scene-1:audio", "audio_producer")
    _tag(state, "clip-0/scene-1", "video_producer")
    _tag(state, "assembler:final", "assembler")
    rec = _append(
        state,
        scope=Scope.GLOBAL,
        subject=Subject.TONE,
        content="warmer overall tone",
    )
    drift = _make_drift(state, stage_name="scenario", from_rev=0, new_records=[rec])

    impacted = analyse_impact(state, drift)
    keys = {i.artifact_key for i in impacted}
    assert keys == {"scene-1:audio", "clip-0/scene-1", "assembler:final"}


def test_stage_record_matches_only_its_stage():
    state = _fresh_state()
    _tag(state, "scene-1:audio", "audio_producer")
    _tag(state, "clip-0/scene-1", "video_producer")
    rec = _append(
        state,
        scope=Scope.STAGE,
        scope_ref="audio_producer",
        subject=Subject.VOICE,
        content="lower voice",
    )
    drift = _make_drift(state, stage_name="audio_producer", from_rev=0, new_records=[rec])

    impacted = analyse_impact(state, drift)
    keys = {i.artifact_key for i in impacted}
    assert keys == {"scene-1:audio"}


def test_scene_record_substring_matches_artifact_key():
    state = _fresh_state()
    _tag(state, "scene-3:audio", "audio_producer")
    _tag(state, "scene-4:audio", "audio_producer")
    rec = _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-3",
        subject=Subject.TONE,
        content="warmer scene 3",
    )
    drift = _make_drift(state, stage_name="audio_producer", from_rev=0, new_records=[rec])

    impacted = analyse_impact(state, drift)
    assert {i.artifact_key for i in impacted} == {"scene-3:audio"}


def test_artifacts_at_or_past_drift_horizon_are_skipped():
    state = _fresh_state()
    # rev 1 lands first; artifact is tagged AFTER, so its revision is 1.
    rec1 = _append(state, scope=Scope.GLOBAL, content="rev1")
    _tag(state, "scene-1:audio", "audio_producer")
    # drift window from rev 0 -> rev 1; the artifact is AT the horizon,
    # so it's considered already-derived.
    drift = LedgerDrift(
        stage_name="scenario",
        artifact_ids=(),
        from_rev=0,
        to_rev=1,
        new_records=(rec1.to_dict(),),
    )
    assert analyse_impact(state, drift) == []


def test_analyse_impact_result_is_sorted_by_artifact_key():
    state = _fresh_state()
    _tag(state, "zeta", "s")
    _tag(state, "alpha", "s")
    _tag(state, "mid", "s")
    rec = _append(state, scope=Scope.GLOBAL, content="global")
    drift = _make_drift(state, stage_name="s", from_rev=0, new_records=[rec])
    impacted = analyse_impact(state, drift)
    assert [i.artifact_key for i in impacted] == ["alpha", "mid", "zeta"]


# ---------------------------------------------------------------------------
# plan_remanifestation
# ---------------------------------------------------------------------------


def test_empty_drift_produces_empty_plan():
    state = _fresh_state()
    rec = _append(state, scope=Scope.GLOBAL, content="none")
    # No tagged artifacts -> empty plan.
    drift = _make_drift(state, stage_name="s", from_rev=0, new_records=[rec])
    plan = plan_remanifestation(state, drift)
    assert plan.is_empty
    assert plan.steps == ()
    assert "no impacted artifacts" in plan.reason


def test_hard_record_on_scene_artifact_yields_rewrite_scene():
    state = _fresh_state()
    _tag(state, "scene-3:audio", "audio_producer")
    rec = _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-3",
        polarity=Polarity.FORBID,
        subject=Subject.TONE,
        content="must not sound ironic",
    )
    drift = _make_drift(state, stage_name="audio_producer", from_rev=0, new_records=[rec])
    plan = plan_remanifestation(state, drift)
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.action == "rewrite_scene"
    assert step.scene_id == "scene-3"
    assert step.guidance


def test_duration_prefer_yields_generate_extension_clip():
    state = _fresh_state()
    _tag(state, "scene-2:video", "video_producer")
    rec = _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-2",
        polarity=Polarity.PREFER,
        subject=Subject.DURATION,
        content="add a beat of breathing room at the end",
    )
    drift = _make_drift(state, stage_name="video_producer", from_rev=0, new_records=[rec])
    plan = plan_remanifestation(state, drift)
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.action == "generate_extension_clip"
    assert step.scene_id == "scene-2"
    assert step.duration_needed and step.duration_needed > 0


def test_default_action_is_regenerate_clip_with_nonzero_seed_delta():
    state = _fresh_state()
    _tag(state, "scene-1/clip-0", "video_producer")
    rec = _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        polarity=Polarity.PREFER,
        subject=Subject.VISUAL_STYLE,
        content="cooler colour palette",
    )
    drift = _make_drift(state, stage_name="video_producer", from_rev=0, new_records=[rec])
    plan = plan_remanifestation(state, drift)
    step = plan.steps[0]
    assert step.action == "regenerate_clip"
    assert step.clip_id == "clip-0"
    assert step.seed_delta and step.seed_delta != 0
    assert step.prompt_delta


def test_plan_allowed_actions_only():
    state = _fresh_state()
    _tag(state, "scene-1:audio", "audio_producer")
    _tag(state, "scene-2/clip-0", "video_producer")
    _tag(state, "opaque-key", "assembler")
    rec = _append(state, scope=Scope.GLOBAL, content="global tone shift")
    drift = _make_drift(state, stage_name="s", from_rev=0, new_records=[rec])
    plan = plan_remanifestation(state, drift)
    actions = {s.action for s in plan.steps}
    assert actions.issubset(ALLOWED_ACTIONS)


# ---------------------------------------------------------------------------
# validate_plan
# ---------------------------------------------------------------------------


def test_validator_rejects_forbidden_legacy_action():
    state = _fresh_state()
    _tag(state, "scene-1:audio", "audio_producer")
    # Hand-craft a plan that names a forbidden action.
    plan = RemanifestationPlan(
        stage_name="audio_producer",
        from_rev=0,
        to_rev=1,
        steps=(
            PlanStep(
                action="trim_narration",
                artifact_key="scene-1:audio",
                reason="test",
                scene_id="scene-1",
            ),
        ),
    )
    with pytest.raises(InvalidPlanError, match="forbidden action"):
        validate_plan(state, plan)


def test_validator_rejects_unknown_artifact_key():
    state = _fresh_state()
    plan = RemanifestationPlan(
        stage_name="audio_producer",
        from_rev=0,
        to_rev=1,
        steps=(
            PlanStep(
                action="rewrite_scene",
                artifact_key="scene-1:audio",
                reason="test",
                scene_id="scene-1",
                guidance="g",
            ),
        ),
    )
    with pytest.raises(InvalidPlanError, match="no tag"):
        validate_plan(state, plan)


def test_validator_rejects_extension_when_duration_is_forbidden():
    state = _fresh_state()
    _tag(state, "scene-2:video", "video_producer")
    _append(
        state,
        scope=Scope.GLOBAL,
        polarity=Polarity.FORBID,
        subject=Subject.DURATION,
        content="keep it tight; no extensions",
    )
    plan = RemanifestationPlan(
        stage_name="video_producer",
        from_rev=0,
        to_rev=1,
        steps=(
            PlanStep(
                action="generate_extension_clip",
                artifact_key="scene-2:video",
                reason="test",
                scene_id="scene-2",
                duration_needed=1.0,
            ),
        ),
    )
    with pytest.raises(InvalidPlanError, match="FORBID"):
        validate_plan(state, plan)


def test_validator_accepts_valid_plan():
    state = _fresh_state()
    _tag(state, "scene-1/clip-0", "video_producer")
    plan = RemanifestationPlan(
        stage_name="video_producer",
        from_rev=0,
        to_rev=1,
        steps=(
            PlanStep(
                action="regenerate_clip",
                artifact_key="scene-1/clip-0",
                reason="test",
                clip_id="clip-0",
                prompt_delta="cooler palette",
                seed_delta=42,
            ),
        ),
    )
    validate_plan(state, plan)  # no exception


def test_validator_rejects_duplicate_artifact_keys():
    state = _fresh_state()
    _tag(state, "scene-1/clip-0", "video_producer")
    plan = RemanifestationPlan(
        stage_name="video_producer",
        from_rev=0,
        to_rev=1,
        steps=(
            PlanStep(
                action="regenerate_clip",
                artifact_key="scene-1/clip-0",
                reason="test",
                clip_id="clip-0",
                prompt_delta="x",
                seed_delta=1,
            ),
            PlanStep(
                action="regenerate_clip",
                artifact_key="scene-1/clip-0",
                reason="test",
                clip_id="clip-0",
                prompt_delta="y",
                seed_delta=2,
            ),
        ),
    )
    with pytest.raises(InvalidPlanError, match="re-targets artifact"):
        validate_plan(state, plan)


# ---------------------------------------------------------------------------
# execute_plan
# ---------------------------------------------------------------------------


def test_execute_plan_queues_steps_and_clears_tags():
    state = _fresh_state()
    _tag(state, "scene-1/clip-0", "video_producer")
    plan = RemanifestationPlan(
        stage_name="video_producer",
        from_rev=0,
        to_rev=1,
        steps=(
            PlanStep(
                action="regenerate_clip",
                artifact_key="scene-1/clip-0",
                reason="test",
                clip_id="clip-0",
                prompt_delta="cooler",
                seed_delta=1,
            ),
        ),
    )
    receipts = execute_plan(state, plan)
    assert len(receipts) == 1
    assert receipts[0]["status"] == "queued"
    # Tag cleared so next producer will re-tag at the new revision.
    assert not has_tag(state, "scene-1/clip-0")
    # Queue + receipts written to blackboard in JSON form.
    queue = json.loads(state[REMANIFESTATION_QUEUE_KEY])
    assert len(queue) == 1
    assert queue[0]["action"] == "regenerate_clip"
    receipts_json = json.loads(state[REMANIFESTATION_RECEIPTS_KEY])
    assert len(receipts_json) == 1


def test_execute_plan_isolates_executor_failures_per_step():
    state = _fresh_state()
    _tag(state, "scene-1/clip-0", "video_producer")
    _tag(state, "scene-2/clip-0", "video_producer")
    plan = RemanifestationPlan(
        stage_name="video_producer",
        from_rev=0,
        to_rev=1,
        steps=(
            PlanStep(
                action="regenerate_clip",
                artifact_key="scene-1/clip-0",
                reason="t",
                clip_id="clip-0",
                prompt_delta="a",
                seed_delta=1,
            ),
            PlanStep(
                action="regenerate_clip",
                artifact_key="scene-2/clip-0",
                reason="t",
                clip_id="clip-0",
                prompt_delta="b",
                seed_delta=1,
            ),
        ),
    )

    call_log: list[str] = []

    def flaky_executor(state, step):
        call_log.append(step.artifact_key)
        if step.artifact_key == "scene-1/clip-0":
            raise RuntimeError("dispatch boom")
        return {"status": "dispatched", "action": step.action}

    receipts = execute_plan(state, plan, executor=flaky_executor)
    assert [r["status"] for r in receipts] == ["failed", "dispatched"]
    # Failed step left its tag in place; succeeded step cleared its tag.
    assert has_tag(state, "scene-1/clip-0")
    assert not has_tag(state, "scene-2/clip-0")


def test_execute_plan_is_idempotent_on_second_pass():
    state = _fresh_state()
    _tag(state, "scene-1/clip-0", "video_producer")
    plan = RemanifestationPlan(
        stage_name="video_producer",
        from_rev=0,
        to_rev=1,
        steps=(
            PlanStep(
                action="regenerate_clip",
                artifact_key="scene-1/clip-0",
                reason="t",
                clip_id="clip-0",
                prompt_delta="a",
                seed_delta=1,
            ),
        ),
    )
    first = execute_plan(state, plan)
    second = execute_plan(state, plan)
    assert len(first) == 1
    assert len(second) == 1
    # No tag, no second-pass clear failure.
    assert not has_tag(state, "scene-1/clip-0")


# ---------------------------------------------------------------------------
# handle_drift orchestration
# ---------------------------------------------------------------------------


def test_handle_drift_consumes_every_queued_signal_on_success():
    state = _fresh_state()
    _tag(state, "scene-1/clip-0", "video_producer")
    rec = _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        subject=Subject.VISUAL_STYLE,
        content="cooler palette",
    )
    # Queue a drift directly (A5's check_consistency round-trip is
    # exercised elsewhere).
    drift = _make_drift(
        state,
        stage_name="video_producer",
        from_rev=0,
        new_records=[rec],
    )
    state[LEDGER_DRIFT_SIGNALS_KEY] = json.dumps([drift.to_dict()])

    outcomes = handle_drift(state)
    assert len(outcomes) == 1
    assert outcomes[0].error is None
    assert pending_drift_signals(state) == []


def test_handle_drift_reenqueues_signals_whose_plans_fail_validation(monkeypatch):
    """When validate_plan rejects a plan, handle_drift must re-queue the
    drift signal so a human can intervene rather than silently dropping it.

    We inject a validator that always raises to exercise the re-enqueue
    path independently of the planner (which is too careful to emit a
    violating plan on its own).
    """
    import callbacks.remanifestation as remanif

    state = _fresh_state()
    _tag(state, "scene-1/clip-0", "video_producer")
    rec = _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        subject=Subject.VISUAL_STYLE,
        content="cooler palette",
    )
    drift = _make_drift(
        state,
        stage_name="video_producer",
        from_rev=0,
        new_records=[rec],
    )
    state[LEDGER_DRIFT_SIGNALS_KEY] = json.dumps([drift.to_dict()])

    def always_fail(state, plan):
        raise remanif.InvalidPlanError("simulated validator rejection")

    monkeypatch.setattr(remanif, "validate_plan", always_fail)

    outcomes = remanif.handle_drift(state)
    assert len(outcomes) == 1
    assert outcomes[0].error == "simulated validator rejection"
    # Drift re-enqueued for human intervention.
    assert len(pending_drift_signals(state)) == 1
    # No execution attempted -> no receipts.
    assert REMANIFESTATION_RECEIPTS_KEY not in state or state.get(
        REMANIFESTATION_RECEIPTS_KEY
    ) in (None, "", "[]")


def test_handle_drift_end_to_end_with_a5_signal():
    """Full chain: A5 detects drift -> A6 plans/validates/executes."""
    state = _fresh_state()
    # Seed a global baseline + tag a stage as derived at that revision.
    _append(state, scope=Scope.GLOBAL, content="baseline tone")
    record_stage_derivation(
        state, "video_producer", artifact_ids=["scene-1/clip-0"]
    )
    # Tag the artifact at the same revision so analyse_impact can see it.
    tag_artifact(state, artifact_key="scene-1/clip-0", stage="video_producer")
    # Append a new scene-scoped record -> ledger revision advances.
    _append(
        state,
        scope=Scope.SCENE,
        scope_ref="scene-1",
        subject=Subject.VISUAL_STYLE,
        content="cooler palette",
    )
    # A5 flags drift.
    drift = check_consistency(state, "video_producer")
    assert drift is not None
    assert drift.to_rev > drift.from_rev

    # A6 picks up and handles.
    outcomes = handle_drift(state)
    assert len(outcomes) == 1
    assert outcomes[0].error is None
    # Artifact tag cleared -> producer will re-tag at new revision.
    assert not has_tag(state, "scene-1/clip-0")
    # Queue empty.
    assert pending_drift_signals(state) == []
