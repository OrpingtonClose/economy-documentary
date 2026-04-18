"""Tests for the infra escalation ladder (ARCH-C2, #141).

Covers the five-level ladder shape from Diagram 8::

    L0 FIX           retry same job on a different healthy worker
    L1 RETRY         recycle suspect + parallel redispatch
    L2 CREATIVE      scale / hot-swap / region / provider
    L3 COLLABORATIVE coordinate with content ladder + budget guard
    L4 HUMAN         terminates at the same dashboard gate as content L4

All tests use an injected :class:`InfraLadderDeps` so the suite runs
with no network, no live InfraAgent, no live provisioner.  The ladder's
fail-loud contract is exercised explicitly — we never rely on silent
defaults.
"""

from __future__ import annotations

import os
import sys

import pytest

# Ensure ``server/`` is on sys.path when pytest is invoked from the repo root.
_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from dataclasses import replace

from recovery import (  # noqa: E402
    HumanEscalationRequest,
    RecoveryLevel,
    RecoveryPolicy,
)
from infra_ladder import (  # noqa: E402
    BLACKBOARD_L0_SUMMARY_KEY,
    BLACKBOARD_L1_SUMMARY_KEY,
    BLACKBOARD_L2_SUMMARY_KEY,
    BLACKBOARD_L3_SUMMARY_KEY,
    BLACKBOARD_L4_SUMMARY_KEY,
    BLACKBOARD_RESULT_KEY,
    BLACKBOARD_STATE_KEY,
    INFRA_POLICY,
    INFRA_SIG_CUDA_ERROR,
    INFRA_SIG_OOM,
    INFRA_SIG_PROVIDER_OUTAGE,
    INFRA_SIG_WORKER_DEATH,
    KNOWN_INFRA_SIGNATURES,
    InfraFailureEvent,
    InfraLadderDeps,
    InfraLadderResult,
    InfraLadderState,
    InfraRecoveryAction,
    infra_l0_fix,
    infra_l1_retry,
    infra_l2_creative,
    infra_l3_collaborative,
    infra_l4_human,
    run_infra_ladder,
    get_infra_ladder_agent,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    job_id: str = "clip_s001_p001",
    worker_url: str = "http://worker-a:8000",
    signature: str = INFRA_SIG_CUDA_ERROR,
    classification: dict | None = None,
    metadata: dict | None = None,
) -> InfraFailureEvent:
    return InfraFailureEvent(
        job_id=job_id,
        worker_url=worker_url,
        failure_signature=signature,
        raw_error="simulated",
        classification=classification,
        metadata=metadata or {},
    )


class _Recorder:
    """Records calls made on the deps so tests can assert on ordering."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __call__(self, name):
        def _wrap(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return _wrap


def _make_deps(
    *,
    healthy_workers: list[str] | None = None,
    recycle_ok: bool = True,
    scale_url: str | None = None,
    swap_url: str | None = None,
    region_url: str | None = None,
    provider_url: str | None = None,
    can_downspec: bool = False,
    budget_ok: bool = True,
    reshape_ok: bool = False,
    escalation_sink: list[HumanEscalationRequest] | None = None,
    recorder: _Recorder | None = None,
) -> InfraLadderDeps:
    """Construct a fully-mocked InfraLadderDeps."""
    healthy = list(healthy_workers or [])
    sink: list[HumanEscalationRequest] = (
        escalation_sink if escalation_sink is not None else []
    )

    def _record(name, ret):
        def _fn(*args, **kwargs):
            if recorder is not None:
                recorder.calls.append((name, args, kwargs))
            return ret
        return _fn

    return InfraLadderDeps(
        get_healthy_workers=_record("get_healthy_workers", healthy),
        recycle_worker=_record("recycle_worker", recycle_ok),
        scale_fleet=_record("scale_fleet", scale_url),
        hot_swap_tier=_record("hot_swap_tier", swap_url),
        change_region=_record("change_region", region_url),
        change_provider=_record("change_provider", provider_url),
        coordinate_with_content=_record(
            "coordinate_with_content",
            {"can_downspec": can_downspec, "note": "mock"},
        ),
        budget_guard=_record(
            "budget_guard",
            {"ok_to_continue": budget_ok, "remaining_usd": 10.0, "note": "mock"},
        ),
        reshape_plan=_record("reshape_plan", reshape_ok),
        submit_human_escalation=lambda req: sink.append(req),
    )


# ---------------------------------------------------------------------------
# Signatures + policy
# ---------------------------------------------------------------------------


def test_known_infra_signatures_vocabulary():
    """All per-Diagram-8 signature strings are in KNOWN_INFRA_SIGNATURES.

    If Diagram 8 grows a new signature, both the vocabulary and this
    assertion should be updated — the point of the test is to catch
    silent drift.
    """
    diagram_signatures = {
        "worker_death", "oom", "cuda_error", "driver_reset",
        "preemption", "cold_start_fail", "network_partition",
        "vram_exhausted", "thermal_throttle", "auth_revoked",
        "billing_trip", "provider_outage", "storage_unreachable",
    }
    assert diagram_signatures <= KNOWN_INFRA_SIGNATURES, (
        f"Missing signatures: {diagram_signatures - KNOWN_INFRA_SIGNATURES}"
    )


def test_infra_policy_has_its_own_budget():
    """INFRA_POLICY is distinct from content policies and has its own budget."""
    assert isinstance(INFRA_POLICY, RecoveryPolicy)
    # All five levels have a budget.
    for level in (
        RecoveryLevel.FIX, RecoveryLevel.RETRY, RecoveryLevel.CREATIVE,
        RecoveryLevel.COLLABORATIVE, RecoveryLevel.HUMAN,
    ):
        assert INFRA_POLICY.get_level_budget(int(level)) >= 1, (
            f"INFRA_POLICY missing budget for L{int(level)}"
        )
    # L4 has exactly one attempt per failure (same gate as content L4,
    # one escalation is enough).
    assert INFRA_POLICY.get_level_budget(int(RecoveryLevel.HUMAN)) == 1


def test_failure_event_is_known_signature():
    """InfraFailureEvent.is_known_signature classifies signatures correctly."""
    assert _make_event(signature=INFRA_SIG_OOM).is_known_signature()
    assert _make_event(signature=INFRA_SIG_WORKER_DEATH).is_known_signature()
    assert not _make_event(signature="weird_new_thing").is_known_signature()


# ---------------------------------------------------------------------------
# Top-level contract: input validation / fail-loud
# ---------------------------------------------------------------------------


def test_run_infra_ladder_rejects_non_event_input():
    with pytest.raises(TypeError):
        run_infra_ladder("not an event")  # type: ignore[arg-type]


def test_run_infra_ladder_rejects_content_classification():
    """If the classifier says 'content', the infra ladder refuses to run."""
    event = _make_event(
        classification={"classification": "content", "confidence": 0.9},
    )
    with pytest.raises(ValueError, match="routed failure as 'content'"):
        run_infra_ladder(event, deps=_make_deps())


def test_run_infra_ladder_accepts_infra_classification():
    """classification='infra' is the happy path."""
    event = _make_event(
        classification={"classification": "infra", "confidence": 0.95},
    )
    deps = _make_deps(healthy_workers=[
        "http://worker-a:8000", "http://worker-b:8000",
    ])
    result = run_infra_ladder(event, deps=deps)
    assert result.success is True
    assert result.terminal_level == int(RecoveryLevel.FIX)


def test_run_infra_ladder_accepts_none_classification():
    """Classification is optional — ARCH-C3 may pass None."""
    event = _make_event(classification=None)
    deps = _make_deps(healthy_workers=[
        "http://worker-a:8000", "http://worker-b:8000",
    ])
    result = run_infra_ladder(event, deps=deps)
    assert result.success is True


def test_run_infra_ladder_rejects_unclear_classification():
    """'unclear' must be resolved by the caller's diagnostic loop — not by us."""
    event = _make_event(
        classification={"classification": "unclear", "confidence": 0.3},
    )
    with pytest.raises(ValueError):
        run_infra_ladder(event, deps=_make_deps())


def test_run_infra_ladder_rejects_invalid_start_level():
    event = _make_event()
    with pytest.raises(ValueError):
        run_infra_ladder(event, deps=_make_deps(), start_level=-1)
    with pytest.raises(ValueError):
        run_infra_ladder(event, deps=_make_deps(), start_level=5)


# ---------------------------------------------------------------------------
# L0 FIX — retry on different healthy worker
# ---------------------------------------------------------------------------


def test_l0_fix_picks_healthy_worker_other_than_suspect():
    event = _make_event(worker_url="http://worker-a:8000")
    deps = _make_deps(healthy_workers=[
        "http://worker-a:8000",  # suspect — must be skipped
        "http://worker-b:8000",
        "http://worker-c:8000",
    ])
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l0_fix(event, state, deps)
    assert action is not None
    assert action.action_type == "retry_on_healthy_worker"
    assert action.level == int(RecoveryLevel.FIX)
    assert action.target_worker_url == "http://worker-b:8000"
    assert action.target_worker_url != event.worker_url
    assert state.attempts_by_level[int(RecoveryLevel.FIX)] == 1


def test_l0_fix_returns_none_when_only_suspect_is_healthy():
    event = _make_event(worker_url="http://worker-a:8000")
    deps = _make_deps(healthy_workers=["http://worker-a:8000"])
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l0_fix(event, state, deps)
    assert action is None
    assert state.escalation_trail, "must record why L0 could not resolve"


def test_l0_fix_returns_none_when_fleet_empty():
    event = _make_event()
    deps = _make_deps(healthy_workers=[])
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    assert infra_l0_fix(event, state, deps) is None


def test_l0_fix_respects_excluded_workers():
    """Subsequent L0 attempts must not re-pick workers that already failed."""
    event = _make_event(worker_url="http://worker-a:8000")
    deps = _make_deps(healthy_workers=[
        "http://worker-a:8000",
        "http://worker-b:8000",
        "http://worker-c:8000",
    ])
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    first = infra_l0_fix(event, state, deps)
    assert first is not None and first.target_worker_url == "http://worker-b:8000"

    second = infra_l0_fix(event, state, deps)
    assert second is not None
    assert second.target_worker_url == "http://worker-c:8000"


# ---------------------------------------------------------------------------
# L1 RETRY — recycle suspect + parallel redispatch
# ---------------------------------------------------------------------------


def test_l1_retry_recycles_suspect_and_redispatches_in_parallel():
    event = _make_event(worker_url="http://worker-a:8000")
    recorder = _Recorder()
    deps = _make_deps(
        healthy_workers=["http://worker-a:8000", "http://worker-b:8000"],
        recycle_ok=True,
        recorder=recorder,
    )
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l1_retry(event, state, deps)
    assert action is not None
    assert action.action_type == "recycle_and_redispatch"
    assert action.level == int(RecoveryLevel.RETRY)
    assert action.target_worker_url == "http://worker-b:8000"
    assert action.recycled_worker_url == "http://worker-a:8000"
    assert action.details["recycle_initiated"] is True
    assert action.details["parallel_redispatch"] is True

    # recycle_worker called before redispatch inspection
    names = [c[0] for c in recorder.calls]
    assert "recycle_worker" in names


def test_l1_retry_records_recycle_even_without_parallel_target():
    """Recycle is attempted even if no parallel target — ladder escalates."""
    event = _make_event(worker_url="http://worker-a:8000")
    deps = _make_deps(
        healthy_workers=["http://worker-a:8000"],  # only suspect is healthy
        recycle_ok=True,
    )
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l1_retry(event, state, deps)
    assert action is None  # escalate
    assert any("L1" in note and "recycle initiated" in note
               for note in state.escalation_trail)


def test_l1_retry_records_failure_when_recycle_fails():
    event = _make_event(worker_url="http://worker-a:8000")
    deps = _make_deps(
        healthy_workers=["http://worker-a:8000"],
        recycle_ok=False,
    )
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l1_retry(event, state, deps)
    assert action is None
    assert any("recycle_worker" in note and "failed" in note
               for note in state.escalation_trail)


# ---------------------------------------------------------------------------
# L2 CREATIVE — scale / hot-swap / region / provider
# ---------------------------------------------------------------------------


def test_l2_creative_prefers_scale_fleet():
    event = _make_event()
    deps = _make_deps(
        scale_url="http://scaled-new:8000",
        swap_url="http://swap-unused:8000",
        region_url="http://region-unused:8000",
        provider_url="http://provider-unused:8000",
    )
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l2_creative(event, state, deps)
    assert action is not None
    assert action.action_type == "scale_fleet"
    assert action.target_worker_url == "http://scaled-new:8000"


def test_l2_creative_falls_back_to_hot_swap_when_scale_fails():
    event = _make_event(metadata={"fallback_tier": "A6000"})
    deps = _make_deps(
        scale_url=None,
        swap_url="http://swapped:8000",
        region_url="http://region-unused:8000",
        provider_url="http://provider-unused:8000",
    )
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l2_creative(event, state, deps)
    assert action is not None
    assert action.action_type == "hot_swap_tier"
    assert action.details["target_tier"] == "A6000"
    assert action.target_worker_url == "http://swapped:8000"


def test_l2_creative_falls_back_to_region_then_provider():
    event = _make_event(metadata={
        "fallback_region": "eu-west",
        "fallback_provider": "runpod",
    })
    deps = _make_deps(
        scale_url=None,
        swap_url=None,
        region_url="http://eu-west:8000",
        provider_url="http://runpod:8000",
    )
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l2_creative(event, state, deps)
    assert action is not None
    assert action.action_type == "change_region"
    assert action.details["target_region"] == "eu-west"


def test_l2_creative_falls_back_to_provider_when_no_region_config():
    event = _make_event(metadata={"fallback_provider": "runpod"})
    deps = _make_deps(
        scale_url=None, swap_url=None, region_url=None,
        provider_url="http://runpod:8000",
    )
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l2_creative(event, state, deps)
    assert action is not None
    assert action.action_type == "change_provider"


def test_l2_creative_returns_none_when_all_strategies_fail():
    event = _make_event()
    deps = _make_deps(
        scale_url=None, swap_url=None, region_url=None, provider_url=None,
    )
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    assert infra_l2_creative(event, state, deps) is None
    assert any("L2" in n for n in state.escalation_trail)


# ---------------------------------------------------------------------------
# L3 COLLABORATIVE — content ladder + budget guard
# ---------------------------------------------------------------------------


def test_l3_collaborative_escalates_when_budget_exhausted():
    event = _make_event()
    deps = _make_deps(budget_ok=False, can_downspec=True, reshape_ok=True)
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l3_collaborative(event, state, deps)
    assert action is None
    assert any("budget guard says stop" in n for n in state.escalation_trail)


def test_l3_collaborative_accepts_content_downspec():
    event = _make_event()
    deps = _make_deps(budget_ok=True, can_downspec=True)
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l3_collaborative(event, state, deps)
    assert action is not None
    assert action.action_type == "coordinate_with_content_ladder"
    assert action.level == int(RecoveryLevel.COLLABORATIVE)


def test_l3_collaborative_falls_back_to_reshape_plan():
    event = _make_event()
    deps = _make_deps(budget_ok=True, can_downspec=False, reshape_ok=True)
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l3_collaborative(event, state, deps)
    assert action is not None
    assert action.action_type == "reshape_production_plan"


def test_l3_collaborative_escalates_when_all_options_fail():
    event = _make_event()
    deps = _make_deps(budget_ok=True, can_downspec=False, reshape_ok=False)
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    assert infra_l3_collaborative(event, state, deps) is None


# ---------------------------------------------------------------------------
# L4 HUMAN — same gate as content L4
# ---------------------------------------------------------------------------


def test_l4_human_submits_through_same_dashboard_gate():
    """L4 submits HumanEscalationRequest via the same submit path as content L4."""
    event = _make_event(signature=INFRA_SIG_PROVIDER_OUTAGE)
    sink: list[HumanEscalationRequest] = []
    deps = _make_deps(escalation_sink=sink)
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    # Pre-populate a couple of earlier attempts so the error_chain is non-empty.
    state.actions_taken.append(
        InfraRecoveryAction(
            action_type="retry_on_healthy_worker",
            level=int(RecoveryLevel.FIX),
            reason="L0 tried worker-b",
            target_worker_url="http://worker-b:8000",
        )
    )

    action = infra_l4_human(event, state, deps)
    assert action.action_type == "escalate_human"
    assert action.level == int(RecoveryLevel.HUMAN)
    assert action.escalation_id is not None
    # A request was submitted via the injected gate.
    assert len(sink) == 1
    req = sink[0]
    assert isinstance(req, HumanEscalationRequest)
    assert req.id == action.escalation_id
    assert req.severity == "critical"
    assert req.operation_name.startswith("infra_ladder:")
    assert req.diagnosis["failure_signature"] == INFRA_SIG_PROVIDER_OUTAGE
    # error_chain contains the earlier attempt
    assert any(a["action_type"] == "retry_on_healthy_worker"
               for a in req.error_chain)


def test_l4_human_is_always_terminal():
    """L4 never returns None — it always emits an escalate_human action."""
    event = _make_event()
    sink: list[HumanEscalationRequest] = []
    deps = _make_deps(escalation_sink=sink)
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    action = infra_l4_human(event, state, deps)
    assert action is not None
    assert action.action_type == "escalate_human"


# ---------------------------------------------------------------------------
# End-to-end ladder walk
# ---------------------------------------------------------------------------


def test_ladder_resolves_at_l0_when_healthy_worker_available():
    event = _make_event(worker_url="http://worker-a:8000")
    sink: list[HumanEscalationRequest] = []
    deps = _make_deps(
        healthy_workers=["http://worker-a:8000", "http://worker-b:8000"],
        escalation_sink=sink,
    )
    result = run_infra_ladder(event, deps=deps)
    assert result.success is True
    assert result.terminal_level == int(RecoveryLevel.FIX)
    assert result.action is not None
    assert result.action.action_type == "retry_on_healthy_worker"
    assert result.action.target_worker_url == "http://worker-b:8000"
    assert sink == []  # no human escalation


def test_ladder_escalates_l0_to_l1_when_no_other_healthy():
    """L0 fails (no other healthy worker) → L1 recycles and still can't find one → escalate."""
    event = _make_event(worker_url="http://worker-a:8000")
    sink: list[HumanEscalationRequest] = []
    deps = _make_deps(
        healthy_workers=["http://worker-a:8000"],  # only suspect is healthy
        recycle_ok=True,
        escalation_sink=sink,
    )
    result = run_infra_ladder(event, deps=deps)
    # L0 returns None; L1 recycles but no redispatch target; L2 has no
    # fallback tier/region/provider; L3 has no downspec/reshape;
    # terminates at L4.
    assert result.terminal_level == int(RecoveryLevel.HUMAN)
    assert result.success is False
    assert result.action is not None
    assert result.action.action_type == "escalate_human"
    assert result.escalation_id is not None
    assert len(sink) == 1


def test_ladder_resolves_at_l1_when_l0_has_no_healthy_but_recycle_yields_parallel():
    """
    If L0 can't find a worker but L1 finds a parallel healthy worker
    after recycle, L1 wins.
    """
    event = _make_event(worker_url="http://worker-a:8000")
    # First call: only suspect in healthy list → L0 returns None.
    # Second call (from L1 looking for parallel): a new worker is healthy.
    healthy_states = [
        ["http://worker-a:8000"],  # L0 call
        ["http://worker-a:8000", "http://worker-b:8000"],  # L1 call
    ]
    call_idx = {"i": 0}

    def _get_healthy():
        i = call_idx["i"]
        call_idx["i"] = min(i + 1, len(healthy_states) - 1)
        return list(healthy_states[i])

    deps = _make_deps(recycle_ok=True)
    deps = replace(deps, get_healthy_workers=_get_healthy)
    result = run_infra_ladder(event, deps=deps)
    assert result.terminal_level == int(RecoveryLevel.RETRY)
    assert result.success is True
    assert result.action is not None
    assert result.action.action_type == "recycle_and_redispatch"


def test_ladder_resolves_at_l2_when_scale_fleet_succeeds():
    event = _make_event(worker_url="http://worker-a:8000")
    deps = _make_deps(
        healthy_workers=["http://worker-a:8000"],
        recycle_ok=False,  # L1 can't recycle
        scale_url="http://new-worker:8000",
    )
    result = run_infra_ladder(event, deps=deps)
    assert result.terminal_level == int(RecoveryLevel.CREATIVE)
    assert result.success is True
    assert result.action is not None
    assert result.action.action_type == "scale_fleet"


def test_ladder_resolves_at_l3_when_content_can_downspec():
    event = _make_event(worker_url="http://worker-a:8000")
    deps = _make_deps(
        healthy_workers=["http://worker-a:8000"],
        recycle_ok=False,
        can_downspec=True,
    )
    result = run_infra_ladder(event, deps=deps)
    assert result.terminal_level == int(RecoveryLevel.COLLABORATIVE)
    assert result.success is True
    assert result.action is not None
    assert result.action.action_type == "coordinate_with_content_ladder"


def test_ladder_terminates_at_l4_with_escalation_id_on_full_exhaustion():
    event = _make_event(
        worker_url="http://worker-a:8000",
        signature=INFRA_SIG_PROVIDER_OUTAGE,
    )
    sink: list[HumanEscalationRequest] = []
    deps = _make_deps(
        healthy_workers=[],  # truly nothing
        escalation_sink=sink,
    )
    result = run_infra_ladder(event, deps=deps)
    assert result.terminal_level == int(RecoveryLevel.HUMAN)
    assert result.success is False
    assert result.escalation_id is not None
    assert result.action is not None
    assert result.action.action_type == "escalate_human"
    # The escalation went through the same gate content L4 uses.
    assert len(sink) == 1
    req = sink[0]
    assert req.id == result.escalation_id
    assert req.severity == "critical"
    # Full trail is reflected in state snapshot.
    trail = result.state_snapshot["escalation_trail"]
    assert any("L0" in n for n in trail)
    assert any("L2" in n or "L3" in n or "L4" in n for n in trail)


# ---------------------------------------------------------------------------
# Budget semantics
# ---------------------------------------------------------------------------


def test_ladder_respects_l0_budget():
    """Budget for L0 is the number of distinct retries we can attempt."""
    event = _make_event(worker_url="http://worker-a:8000")
    # Create a policy with only L0 available (budget 2), everything else 0.
    tight = RecoveryPolicy(
        level_budgets={
            int(RecoveryLevel.FIX): 2,
            int(RecoveryLevel.RETRY): 0,
            int(RecoveryLevel.CREATIVE): 0,
            int(RecoveryLevel.COLLABORATIVE): 0,
            int(RecoveryLevel.HUMAN): 1,
        },
        escalate_to_human=True,
    )
    deps = _make_deps(healthy_workers=[
        "http://worker-a:8000",
        "http://worker-b:8000",
    ])
    state = InfraLadderState(event=event, policy=tight)

    first = infra_l0_fix(event, state, deps)
    assert first is not None
    assert state.budget_remaining(int(RecoveryLevel.FIX)) == 1
    # Second attempt: no other healthy workers available (b already
    # excluded), so L0 returns None.
    second = infra_l0_fix(event, state, deps)
    assert second is None
    assert state.budget_remaining(int(RecoveryLevel.FIX)) == 0


def test_ladder_skips_level_when_budget_zero():
    """When a level's budget is 0 the ladder skips straight past it."""
    event = _make_event(worker_url="http://worker-a:8000")
    # Budget: L0=0 (skip), L1=0 (skip), L2=1, L3=0, L4=1.
    policy = RecoveryPolicy(
        level_budgets={
            int(RecoveryLevel.FIX): 0,
            int(RecoveryLevel.RETRY): 0,
            int(RecoveryLevel.CREATIVE): 1,
            int(RecoveryLevel.COLLABORATIVE): 0,
            int(RecoveryLevel.HUMAN): 1,
        },
        escalate_to_human=True,
    )
    deps = _make_deps(
        healthy_workers=["http://worker-a:8000", "http://worker-b:8000"],
        scale_url="http://scaled:8000",
    )
    result = run_infra_ladder(event, policy=policy, deps=deps)
    assert result.terminal_level == int(RecoveryLevel.CREATIVE)
    assert result.success is True


# ---------------------------------------------------------------------------
# Blackboard contract — keys written by the ladder
# ---------------------------------------------------------------------------


def test_ladder_result_snapshot_is_dict_shaped():
    """The result's state_snapshot is a plain JSON-friendly dict."""
    event = _make_event()
    deps = _make_deps(healthy_workers=["http://a", "http://b"])
    # event.worker_url defaults to http://worker-a:8000; candidates
    # excludes that, so L0 picks http://a or http://b.  Either way,
    # success.
    result = run_infra_ladder(event, deps=deps)
    snap = result.state_snapshot
    assert isinstance(snap, dict)
    assert "event" in snap
    assert "attempts_by_level" in snap
    assert "actions_taken" in snap
    assert "escalation_trail" in snap
    # attempts_by_level values are int
    assert all(isinstance(v, int) for v in snap["attempts_by_level"].values())


# ---------------------------------------------------------------------------
# ADK agent surface (optional — only checked if google-adk is importable)
# ---------------------------------------------------------------------------


def test_adk_agent_lazy_build_or_none():
    """Importing the ADK agent either succeeds or returns None — never raises."""
    agent = get_infra_ladder_agent()
    if agent is None:
        pytest.skip("google-adk not importable in this environment")
    # Top-level agent name
    assert agent.name == "infra_ladder"
    # after_agent_callback is wired (Timeline-Guardian-style)
    assert agent.after_agent_callback is not None


def test_adk_agent_refuses_missing_failure_event():
    """ADK agent raises if state doesn't carry an infra_failure_event."""
    agent = get_infra_ladder_agent()
    if agent is None:
        pytest.skip("google-adk not importable in this environment")
    # Build a minimal stub session / context
    class _StubSession:
        def __init__(self):
            self.state = {}  # no infra_failure_event
    class _StubCtx:
        def __init__(self):
            self.session = _StubSession()

    import asyncio

    async def _drive():
        gen = agent._run_async_impl(_StubCtx())  # type: ignore[attr-defined]
        await gen.__anext__()

    with pytest.raises(RuntimeError, match="infra_failure_event"):
        asyncio.get_event_loop().run_until_complete(_drive()) if False else asyncio.new_event_loop().run_until_complete(_drive())


def test_blackboard_keys_are_distinct():
    """Per-level summary keys are all distinct — no clashes on the blackboard."""
    keys = {
        BLACKBOARD_STATE_KEY,
        BLACKBOARD_RESULT_KEY,
        BLACKBOARD_L0_SUMMARY_KEY,
        BLACKBOARD_L1_SUMMARY_KEY,
        BLACKBOARD_L2_SUMMARY_KEY,
        BLACKBOARD_L3_SUMMARY_KEY,
        BLACKBOARD_L4_SUMMARY_KEY,
    }
    assert len(keys) == 7


# ---------------------------------------------------------------------------
# Reuse of RecoveryLevel / RecoveryPolicy
# ---------------------------------------------------------------------------


def test_actions_use_recovery_level_enum_values():
    """Every action's level field maps to a canonical RecoveryLevel."""
    valid_levels = {
        int(RecoveryLevel.FIX),
        int(RecoveryLevel.RETRY),
        int(RecoveryLevel.CREATIVE),
        int(RecoveryLevel.COLLABORATIVE),
        int(RecoveryLevel.HUMAN),
    }
    event = _make_event(worker_url="http://worker-a:8000")
    deps = _make_deps(healthy_workers=[
        "http://worker-a:8000", "http://worker-b:8000",
    ])
    state = InfraLadderState(event=event, policy=INFRA_POLICY)
    a0 = infra_l0_fix(event, state, deps)
    assert a0 is not None
    assert a0.level in valid_levels
    assert a0.level == int(RecoveryLevel.FIX)


def test_infra_policy_is_instance_of_recovery_policy():
    """Shape reuse: INFRA_POLICY is exactly the content ladder's dataclass."""
    assert isinstance(INFRA_POLICY, RecoveryPolicy)
    # Accepts the legacy fields too (shape-compat), without requiring them.
    assert hasattr(INFRA_POLICY, "escalate_to_human")
    assert hasattr(INFRA_POLICY, "human_timeout_sec")


def test_infra_ladder_result_to_dict_roundtrip():
    event = _make_event(worker_url="http://worker-a:8000")
    deps = _make_deps(healthy_workers=["http://worker-a:8000", "http://worker-b:8000"])
    result = run_infra_ladder(event, deps=deps)
    d = result.to_dict()
    assert d["success"] is True
    assert d["terminal_level"] == int(RecoveryLevel.FIX)
    assert d["action"] is not None
    assert d["action"]["action_type"] == "retry_on_healthy_worker"


# ---------------------------------------------------------------------------
# Fail-loud: tool crashes don't swallow — they record and escalate
# ---------------------------------------------------------------------------


def test_ladder_records_tool_exception_and_escalates():
    """If a tool raises, the ladder records the crash and moves to next level."""
    event = _make_event(worker_url="http://worker-a:8000")

    def _boom():
        raise RuntimeError("boom from get_healthy_workers")

    # Use replace to override one dep
    base = _make_deps()
    deps = replace(base, get_healthy_workers=_boom)

    result = run_infra_ladder(event, deps=deps)
    # L0 crashed (get_healthy_workers raised) → L1 also uses
    # get_healthy_workers but wrapped exception should not kill the
    # ladder — it terminates at L4.
    assert result.terminal_level == int(RecoveryLevel.HUMAN)
    trail = result.state_snapshot["escalation_trail"]
    assert any("RuntimeError" in n for n in trail), (
        f"Expected RuntimeError trail note, got: {trail}"
    )
