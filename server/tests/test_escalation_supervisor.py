"""Unit tests for :mod:`agents.escalation_supervisor`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_server = Path(__file__).resolve().parent.parent
if str(_server) not in sys.path:
    sys.path.insert(0, str(_server))

from agents.escalation_supervisor import (  # noqa: E402
    READ_TOOLS_BY_NAME,
    ReadToolSpec,
    build_user_prompt,
    get_scope_counters,
    reset_scope_counters,
    route_context_through_scope,
    set_supervisor_runner,
    supervisor_escalate_scope,
)
from critique.store import ArtifactCritiqueStore  # noqa: E402
from orchestrator.escalation_menu import (  # noqa: E402
    ACTION_NAMES,
    CREATIVE_ACTION_NAMES,
    EscalationAction,
    EscalationContext,
    OPS_ACTION_NAMES,
)
from orchestrator.escalation_scope import EscalationScope  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_counters() -> None:
    reset_scope_counters()
    yield
    reset_scope_counters()


@pytest.fixture
def tmp_store(tmp_path: Path) -> ArtifactCritiqueStore:
    return ArtifactCritiqueStore(root=tmp_path / "critiques", b2_enabled=False)


@pytest.fixture
def qa_scope() -> EscalationScope:
    return EscalationScope(
        failure_kind="qa_fail",
        trigger_message="clip c-1 failed gatekeeper on sync_drift",
        stage_name="production",
        primary_artifact_id="c-1",
        primary_artifact_type="clip",
        scope_tags=["jury_split"],
        summary_counters={"regen_count": 1, "qa_fail_streak": 1},
    )


# ---------------------------------------------------------------------------
# Read-tool registry
# ---------------------------------------------------------------------------

def test_read_tools_include_every_expected_name() -> None:
    expected = {
        "read_artifact_critique_history",
        "read_qa_verdicts",
        "read_escalation_history",
        "read_worker_health",
        "read_stage_timing",
        "read_infra_status_snapshot",
        "read_infra_escalation_log",
        "read_vast_cost_snapshot",
        "read_timeline_state",
        "read_artifact_record",
    }
    assert set(READ_TOOLS_BY_NAME) == expected
    for spec in READ_TOOLS_BY_NAME.values():
        assert isinstance(spec, ReadToolSpec)
        assert spec.description
        assert spec.parameters["type"] == "object"
        assert callable(spec.fn)


def test_build_user_prompt_is_deterministic_and_mentions_scope_id(
    qa_scope: EscalationScope,
) -> None:
    first = build_user_prompt(qa_scope)
    second = build_user_prompt(qa_scope)
    assert first == second
    assert qa_scope.scope_id in first
    assert "EscalationAction" in first


# ---------------------------------------------------------------------------
# supervisor_escalate_scope with injected runner
# ---------------------------------------------------------------------------

def test_scope_path_returns_runner_action_and_records_ref(
    qa_scope: EscalationScope, tmp_store: ArtifactCritiqueStore
) -> None:
    def fake_runner(
        scope: EscalationScope,
        system: str,
        tools: dict[str, ReadToolSpec],
        model: str,
    ) -> EscalationAction:
        # Exercise a read-tool dispatch via the registry.
        tools["read_worker_health"].fn()
        return EscalationAction(
            action="regenerate_clip",
            clip_id=scope.primary_artifact_id or "c-1",
            prompt_delta="tighten lip sync",
            seed_delta=17,
            llm_model=model,
            llm_reasoning="jury flagged mouth sync; regenerate with new seed",
        )

    prev = set_supervisor_runner(fake_runner)
    try:
        action = supervisor_escalate_scope(qa_scope, store=tmp_store)
    finally:
        set_supervisor_runner(prev)

    assert action.action == "regenerate_clip"
    assert action.clip_id == "c-1"
    assert action.llm_model  # default model populated if runner left it blank

    snap = get_scope_counters()
    assert snap["escalations_per_run"] == 1
    assert snap["recorded_refs_per_run"] == 1

    record = tmp_store.read("clip", "c-1")
    assert record is not None
    assert len(record.escalations) == 1
    ref = record.escalations[0]
    assert ref.scope_id == qa_scope.scope_id
    assert ref.action == "regenerate_clip"
    # Structured decision metadata is embedded in reasoning.
    assert "--- details ---" in ref.reasoning
    _, _, payload = ref.reasoning.partition("--- details ---")
    details = json.loads(payload.strip())
    assert details["failure_kind"] == "qa_fail"
    assert details["action"] == "regenerate_clip"
    assert details["level"] == action.level
    assert details["action_params"]["clip_id"] == "c-1"


def test_scope_path_ops_action_survives_roundtrip(
    tmp_store: ArtifactCritiqueStore,
) -> None:
    scope = EscalationScope(
        failure_kind="worker_degraded",
        trigger_message="worker http://w1 has 3 consecutive OOMs",
        stage_name="production",
        primary_artifact_id=None,
        primary_artifact_type=None,
        scope_tags=["vram_pressure"],
        summary_counters={"consec_failures": 3},
    )

    def fake_runner(*_args: Any, **_kwargs: Any) -> EscalationAction:
        return EscalationAction(
            action="recycle_worker",
            worker_url="http://w1",
            reason="vram_pressure + 3 consecutive OOMs",
        )

    prev = set_supervisor_runner(fake_runner)
    try:
        action = supervisor_escalate_scope(scope, store=tmp_store)
    finally:
        set_supervisor_runner(prev)

    assert action.action == "recycle_worker"
    assert action.action in OPS_ACTION_NAMES
    # No artifact id in scope → nothing recorded.
    assert get_scope_counters()["recorded_refs_per_run"] == 0


def test_scope_path_runner_exception_falls_back_to_abort(
    qa_scope: EscalationScope, tmp_store: ArtifactCritiqueStore
) -> None:
    def angry_runner(*_args: Any, **_kwargs: Any) -> EscalationAction:
        raise RuntimeError("network hiccup")

    prev = set_supervisor_runner(angry_runner)
    try:
        action = supervisor_escalate_scope(qa_scope, store=tmp_store)
    finally:
        set_supervisor_runner(prev)

    assert action.action == "abort_run"
    assert qa_scope.scope_id in action.reason
    assert get_scope_counters()["fallback_decisions_per_run"] == 1
    # Fallback action is still recorded on the primary artifact.
    assert get_scope_counters()["recorded_refs_per_run"] == 1


def test_scope_path_runner_can_return_any_canonical_action(
    qa_scope: EscalationScope, tmp_store: ArtifactCritiqueStore
) -> None:
    # Smoke-test every canonical action name round-trips through the runner path.
    minimal_payloads: dict[str, dict[str, Any]] = {
        "regenerate_clip": {
            "clip_id": "c-1", "prompt_delta": "x", "seed_delta": 1,
        },
        "generate_extension_clip": {
            "scene_id": "s-1", "duration_needed": 2.0,
        },
        "replace_with_brand_card": {"scene_id": "s-1"},
        "rewrite_scene": {
            "scene_id": "s-1", "guidance": "tighten",
        },
        "abort_run": {"reason": "unrecoverable"},
        "recycle_worker": {"worker_url": "http://w", "reason": "r"},
        "provision_extra_worker": {"role": "video", "count": 1},
        "wait_for_worker_recovery": {
            "worker_url": "http://w", "timeout_sec": 10.0,
        },
        "freeze_batch_and_replan": {"reason": "overbudget"},
    }
    assert set(minimal_payloads) == set(ACTION_NAMES)

    for name, params in minimal_payloads.items():
        def runner(*_a: Any, _payload: dict[str, Any] = params,
                   _name: str = name, **_kw: Any) -> EscalationAction:
            return EscalationAction(action=_name, **_payload)  # type: ignore[arg-type]

        prev = set_supervisor_runner(runner)
        try:
            action = supervisor_escalate_scope(qa_scope, store=tmp_store)
        finally:
            set_supervisor_runner(prev)
        assert action.action == name


# ---------------------------------------------------------------------------
# route_context_through_scope bridge
# ---------------------------------------------------------------------------

def test_route_context_through_scope_uses_scope_path(
    tmp_store: ArtifactCritiqueStore,
) -> None:
    captured: dict[str, Any] = {}

    def fake_runner(scope: EscalationScope, *_a: Any, **_kw: Any) -> EscalationAction:
        captured["scope"] = scope
        return EscalationAction(action="abort_run", reason="stub")

    ctx = EscalationContext(
        failing_artifact="clip c-9 keeps failing jury",
        artifact_descriptor={"clip_id": "c-9"},
        timeline_state_snapshot={},
        escalation_history=[],
        high_cost=False,
    )

    prev = set_supervisor_runner(fake_runner)
    try:
        action = route_context_through_scope(
            ctx,
            failure_kind="qa_fail",
            stage_name="production",
            primary_artifact_id="c-9",
            primary_artifact_type="clip",
            scope_tags=["jury_split"],
            store=tmp_store,
        )
    finally:
        set_supervisor_runner(prev)

    assert action.action == "abort_run"
    assert captured["scope"].primary_artifact_id == "c-9"
    assert captured["scope"].failure_kind == "qa_fail"
    assert captured["scope"].trigger_message == ctx.failing_artifact
    assert "jury_split" in captured["scope"].scope_tags


# ---------------------------------------------------------------------------
# Sanity: creative vs ops action partition
# ---------------------------------------------------------------------------

def test_creative_and_ops_partitions_cover_all_action_names() -> None:
    assert set(CREATIVE_ACTION_NAMES).isdisjoint(OPS_ACTION_NAMES)
    assert set(CREATIVE_ACTION_NAMES) | set(OPS_ACTION_NAMES) == set(ACTION_NAMES)
