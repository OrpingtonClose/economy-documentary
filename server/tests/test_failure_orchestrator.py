"""Tests for the ARCH-C3 failure orchestrator (#142).

Covers:
    - ``content`` failures dispatch to the creative supervisor and NEVER
      come back carrying an infra_action (no silent downgrade).
    - ``infra`` failures dispatch to the infra ladder; L4 terminal
      surfaces ``escalation_id`` against the same dashboard gate content
      L4 uses.
    - ``unclear`` first-pass triggers the diagnostic loop; if telemetry
      enrichment flips the classification, the corresponding ladder runs.
    - ``unclear`` after ``max_diagnostic_rounds`` goes straight to L4
      (no silent retry forever).
    - Fail-loud invariants: non-creative action from supervisor, non-infra
      action from the ladder, missing escalation_id on L4 — all raise
      :class:`FailureOrchestratorError`.
    - Blackboard writes land under ``failure_orchestrator_result`` /
      ``failure_orchestrator_summary``.
    - ``_CANONICAL_TO_CALLER`` is NEVER applied on the push path — the
      orchestrator surfaces the canonical action name verbatim.
"""

from __future__ import annotations

import os
import sys

import pytest

_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from agents.diagnostic_classifier import (  # noqa: E402
    CLASSIFICATION_CONTENT,
    CLASSIFICATION_INFRA,
    CLASSIFICATION_UNCLEAR,
    Classification,
    FailureEvent,
    InfraTelemetry,
)
from failure_orchestrator import (  # noqa: E402
    BLACKBOARD_DIAGNOSTIC_TRAIL_KEY,
    BLACKBOARD_RESULT_KEY,
    BLACKBOARD_SUMMARY_KEY,
    FailureOrchestratorDeps,
    FailureOrchestratorError,
    OrchestratorDecision,
    RESOLUTION_CONTENT,
    RESOLUTION_INFRA,
    RESOLUTION_UNCLEAR_TIMEOUT,
    ROUTE_CONTENT_LADDER,
    ROUTE_HUMAN_ESCALATION,
    ROUTE_INFRA_LADDER,
    _assert_no_silent_downgrade,
    _infer_infra_signature,
    route_failure,
)
from infra_ladder import (  # noqa: E402
    InfraFailureEvent,
    InfraLadderResult,
    InfraRecoveryAction,
)
from orchestrator.escalation_menu import EscalationAction  # noqa: E402
from recovery import HumanEscalationRequest, RecoveryLevel  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_event(**overrides) -> FailureEvent:
    defaults: dict = {
        "operation_name": "video_generation",
        "error_message": "",
        "exception_type": "",
        "stack_trace": "",
        "worker_id": "http://worker-a:8000",
        "qa_verdict": "",
        "qa_reason": "",
        "pipeline_stage": "production",
    }
    defaults.update(overrides)
    return FailureEvent(**defaults)


def _classification(kind: str, *, confidence: float = 0.9, signals=None) -> Classification:
    return Classification(
        classification=kind,
        reasoning=f"stub classifier decided {kind}",
        confidence=confidence,
        signals=list(signals or []),
        source="heuristic",
    )


class _ClassifyStub:
    """Stub classifier: returns successive classifications per call.

    Records everything so tests can assert the diagnostic loop was
    actually invoked.  Unlike the real classifier, it never raises —
    the orchestrator's loop cap is what must force termination.
    """

    def __init__(self, results: list[Classification]) -> None:
        self._results = list(results)
        self.calls: list[tuple[FailureEvent, InfraTelemetry | None, bool]] = []

    def __call__(self, event, telemetry, use_llm, state):
        self.calls.append((event, telemetry, use_llm))
        if not self._results:
            # Repeat the last result rather than raising — keeps the
            # test deterministic if the loop overshoots.
            return self.calls[-1][0] and Classification(
                classification=CLASSIFICATION_UNCLEAR,
                reasoning="stub exhausted",
                confidence=0.1,
                signals=[],
                source="heuristic",
            )
        return self._results.pop(0)


def _noop_enrich(event, existing):
    """Default enrich stub: returns the same telemetry (no new evidence)."""
    return existing


def _never_called(*args, **kwargs):
    raise AssertionError("this dep should not have been called")


def _next_id_factory(prefix: str = "esc-test"):
    counter = {"n": 0}

    def _next_id() -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']:04d}"

    return _next_id


def _make_deps(
    *,
    classify,
    enrich=_noop_enrich,
    run_infra=_never_called,
    supervisor=_never_called,
    submit=None,
    next_id=None,
) -> FailureOrchestratorDeps:
    submitted: list[HumanEscalationRequest] = []

    def _capture(req: HumanEscalationRequest) -> None:
        submitted.append(req)

    deps = FailureOrchestratorDeps(
        classify=classify,
        enrich_telemetry=enrich,
        run_infra_ladder=run_infra,
        supervisor_escalate=supervisor,
        submit_human_escalation=submit or _capture,
        next_escalation_id=next_id or _next_id_factory(),
    )
    # Stash the capture list on the deps for test assertions.
    deps._submitted_for_tests = submitted if submit is None else []  # type: ignore[attr-defined]
    return deps


# ---------------------------------------------------------------------------
# content path
# ---------------------------------------------------------------------------


def test_content_classification_dispatches_to_supervisor():
    captured: dict[str, object] = {}

    def fake_supervisor(context):
        captured["context"] = context
        return EscalationAction(
            action="regenerate_clip",
            clip_id="s001_p002",
            prompt_delta="emphasise kitchen setting",
            seed_delta=7,
            llm_model="gemini-flash",
            llm_reasoning="bad prompt",
        )

    deps = _make_deps(
        classify=_ClassifyStub([_classification(CLASSIFICATION_CONTENT)]),
        supervisor=fake_supervisor,
    )
    event = _mk_event(
        operation_name="clip_s001_p002",
        error_message="QA rejected: off-topic",
        qa_verdict="rejected",
        qa_reason="off-topic — shows farm instead of kitchen",
    )
    decision = route_failure(event, deps=deps, use_llm=False)

    assert decision.route == ROUTE_CONTENT_LADDER
    assert decision.resolution == RESOLUTION_CONTENT
    assert decision.creative_action is not None
    assert decision.creative_action["action"] == "regenerate_clip"
    assert decision.infra_action is None
    assert decision.escalation_id is None
    # Context forwarded to the supervisor carried the classifier signal.
    assert captured["context"].failing_artifact == "clip_s001_p002"


def test_content_abort_run_stays_on_content_route():
    """``abort_run`` is a CREATIVE action; it must not be re-routed to L4."""

    def fake_supervisor(context):
        return EscalationAction(action="abort_run", reason="structural")

    deps = _make_deps(
        classify=_ClassifyStub([_classification(CLASSIFICATION_CONTENT)]),
        supervisor=fake_supervisor,
    )
    decision = route_failure(_mk_event(qa_verdict="rejected"), deps=deps, use_llm=False)
    assert decision.route == ROUTE_CONTENT_LADDER
    assert decision.creative_action["action"] == "abort_run"
    assert decision.escalation_id is None  # not an L4 gate path


def test_content_supervisor_returning_non_creative_action_raises():
    """Fail-loud: supervisor returning a non-``ActionName`` is a contract break."""

    class BadAction:  # not even an EscalationAction
        pass

    deps = _make_deps(
        classify=_ClassifyStub([_classification(CLASSIFICATION_CONTENT)]),
        supervisor=lambda ctx: BadAction(),
    )
    with pytest.raises(FailureOrchestratorError, match="EscalationAction"):
        route_failure(_mk_event(qa_verdict="rejected"), deps=deps, use_llm=False)


def test_content_canonical_to_caller_is_not_applied_on_push_path():
    """``_CANONICAL_TO_CALLER`` would map ``regenerate_clip`` -> ``retry_with_fix``.

    The push-path orchestrator must surface the CANONICAL action name,
    not the caller shim.  Closes c697525 rationale (PR #117 branch).
    """

    def fake_supervisor(context):
        return EscalationAction(
            action="regenerate_clip",
            clip_id="s001_p001",
            prompt_delta="retry with different angle",
            seed_delta=-3,
        )

    deps = _make_deps(
        classify=_ClassifyStub([_classification(CLASSIFICATION_CONTENT)]),
        supervisor=fake_supervisor,
    )
    decision = route_failure(_mk_event(qa_verdict="rejected"), deps=deps, use_llm=False)
    # Canonical name is preserved; we do NOT see "retry_with_fix".
    assert decision.creative_action["action"] == "regenerate_clip"
    assert "retry_with_fix" not in str(decision.to_dict())


# ---------------------------------------------------------------------------
# infra path
# ---------------------------------------------------------------------------


def test_infra_classification_dispatches_to_infra_ladder():
    captured: dict[str, object] = {}

    def fake_ladder(event: InfraFailureEvent) -> InfraLadderResult:
        captured["event"] = event
        action = InfraRecoveryAction(
            action_type="retry_on_healthy_worker",
            level=int(RecoveryLevel.FIX),
            reason="retry on healthy worker-b",
            target_worker_url="http://worker-b:8000",
        )
        return InfraLadderResult(
            success=True,
            terminal_level=int(RecoveryLevel.FIX),
            action=action,
            state_snapshot={},
        )

    deps = _make_deps(
        classify=_ClassifyStub([_classification(CLASSIFICATION_INFRA)]),
        run_infra=fake_ladder,
    )
    event = _mk_event(
        operation_name="clip_s003_p004",
        error_message="CUDA error: device-side assert",
        exception_type="RuntimeError",
    )
    decision = route_failure(event, deps=deps, use_llm=False)

    assert decision.route == ROUTE_INFRA_LADDER
    assert decision.resolution == RESOLUTION_INFRA
    assert decision.infra_action is not None
    assert decision.infra_action["action_type"] == "retry_on_healthy_worker"
    assert decision.infra_terminal_level == int(RecoveryLevel.FIX)
    assert decision.creative_action is None
    # The constructed InfraFailureEvent carried the classifier result.
    infra_event: InfraFailureEvent = captured["event"]
    assert infra_event.classification["classification"] == CLASSIFICATION_INFRA
    assert infra_event.failure_signature == "cuda_error"


def test_infra_ladder_l4_surfaces_escalation_id():
    def fake_ladder(event: InfraFailureEvent) -> InfraLadderResult:
        action = InfraRecoveryAction(
            action_type="escalate_human",
            level=int(RecoveryLevel.HUMAN),
            reason="exhausted",
            escalation_id="esc-1234-infra",
        )
        return InfraLadderResult(
            success=False,
            terminal_level=int(RecoveryLevel.HUMAN),
            action=action,
            state_snapshot={},
            escalation_id="esc-1234-infra",
        )

    deps = _make_deps(
        classify=_ClassifyStub([_classification(CLASSIFICATION_INFRA)]),
        run_infra=fake_ladder,
    )
    decision = route_failure(
        _mk_event(error_message="process crashed"),
        deps=deps,
        use_llm=False,
    )
    assert decision.route == ROUTE_HUMAN_ESCALATION
    assert decision.resolution == RESOLUTION_INFRA
    assert decision.escalation_id == "esc-1234-infra"
    assert decision.infra_action is not None
    assert decision.creative_action is None


def test_infra_ladder_returning_failure_without_l4_raises():
    """Fail-loud: ``success=False`` at a non-L4 level is a contract break."""

    def fake_ladder(event: InfraFailureEvent) -> InfraLadderResult:
        return InfraLadderResult(
            success=False,
            terminal_level=int(RecoveryLevel.CREATIVE),
            action=None,
            state_snapshot={},
        )

    deps = _make_deps(
        classify=_ClassifyStub([_classification(CLASSIFICATION_INFRA)]),
        run_infra=fake_ladder,
    )
    with pytest.raises(FailureOrchestratorError, match="expected L4"):
        route_failure(
            _mk_event(error_message="cuda error"), deps=deps, use_llm=False
        )


def test_infra_ladder_l4_missing_escalation_id_raises():
    def fake_ladder(event: InfraFailureEvent) -> InfraLadderResult:
        return InfraLadderResult(
            success=False,
            terminal_level=int(RecoveryLevel.HUMAN),
            action=InfraRecoveryAction(
                action_type="escalate_human",
                level=int(RecoveryLevel.HUMAN),
                reason="exhausted",
                escalation_id=None,  # violation
            ),
            state_snapshot={},
            escalation_id=None,  # violation
        )

    deps = _make_deps(
        classify=_ClassifyStub([_classification(CLASSIFICATION_INFRA)]),
        run_infra=fake_ladder,
    )
    with pytest.raises(FailureOrchestratorError, match="escalation_id"):
        route_failure(
            _mk_event(error_message="cuda error"), deps=deps, use_llm=False
        )


# ---------------------------------------------------------------------------
# unclear / diagnostic loop
# ---------------------------------------------------------------------------


def test_unclear_then_content_after_enrichment():
    """First pass is unclear; enrichment gives a definitive content signal."""

    def enrich(event, existing):
        # Simulate the infra_agent saying "this worker is fine" — so
        # the second-round classifier sees no infra noise and decides content.
        return InfraTelemetry(worker_status="healthy")

    classifier = _ClassifyStub([
        _classification(CLASSIFICATION_UNCLEAR, confidence=0.5),
        _classification(CLASSIFICATION_CONTENT),
    ])

    def fake_supervisor(context):
        return EscalationAction(
            action="rewrite_scene",
            scene_id="s004",
            guidance="rework hook",
        )

    deps = _make_deps(
        classify=classifier,
        enrich=enrich,
        supervisor=fake_supervisor,
    )
    decision = route_failure(
        _mk_event(error_message="ambiguous", qa_reason="maybe off-topic"),
        deps=deps,
        use_llm=False,
    )
    assert len(classifier.calls) == 2  # first + one loop round
    assert decision.route == ROUTE_CONTENT_LADDER
    assert decision.creative_action["action"] == "rewrite_scene"
    assert len(decision.diagnostic_trail) == 2


def test_unclear_then_infra_after_enrichment():
    classifier = _ClassifyStub([
        _classification(CLASSIFICATION_UNCLEAR),
        _classification(CLASSIFICATION_INFRA),
    ])

    def enrich(event, existing):
        return InfraTelemetry(worker_status="unreachable", consecutive_failures=5)

    def fake_ladder(event: InfraFailureEvent) -> InfraLadderResult:
        return InfraLadderResult(
            success=True,
            terminal_level=int(RecoveryLevel.RETRY),
            action=InfraRecoveryAction(
                action_type="recycle_and_redispatch",
                level=int(RecoveryLevel.RETRY),
                reason="recycle suspect",
                target_worker_url="http://worker-c:8000",
                recycled_worker_url="http://worker-a:8000",
            ),
            state_snapshot={},
        )

    deps = _make_deps(
        classify=classifier,
        enrich=enrich,
        run_infra=fake_ladder,
    )
    decision = route_failure(
        _mk_event(error_message="ambiguous crash"),
        deps=deps,
        use_llm=False,
    )
    assert decision.route == ROUTE_INFRA_LADDER
    assert decision.infra_action["action_type"] == "recycle_and_redispatch"


def test_unclear_timeout_escalates_to_l4():
    """After ``max_diagnostic_rounds`` unclear in a row, escalate to L4."""

    classifier = _ClassifyStub([
        _classification(CLASSIFICATION_UNCLEAR),
        _classification(CLASSIFICATION_UNCLEAR),
        _classification(CLASSIFICATION_UNCLEAR),
    ])

    # Enrichment returns different telemetry each round so the loop does
    # not short-circuit on "no new evidence".
    call_count = {"n": 0}

    def enrich(event, existing):
        call_count["n"] += 1
        return InfraTelemetry(
            worker_status="healthy",
            consecutive_failures=call_count["n"],
        )

    deps = _make_deps(
        classify=classifier,
        enrich=enrich,
    )
    decision = route_failure(
        _mk_event(error_message="totally ambiguous"),
        deps=deps,
        max_diagnostic_rounds=2,
        use_llm=False,
    )
    assert len(classifier.calls) == 3  # first + 2 loop rounds
    assert decision.route == ROUTE_HUMAN_ESCALATION
    assert decision.resolution == RESOLUTION_UNCLEAR_TIMEOUT
    assert decision.escalation_id is not None
    assert decision.escalation_id.startswith("esc-test-")
    # Same L4 gate as both ladders — the submit dep was called.
    submitted = deps._submitted_for_tests
    assert len(submitted) == 1
    assert submitted[0].severity == "critical"
    assert "Diagnostic classifier could not" in submitted[0].diagnosis["root_cause"]


def test_max_diagnostic_rounds_zero_escalates_immediately():
    classifier = _ClassifyStub([_classification(CLASSIFICATION_UNCLEAR)])

    deps = _make_deps(
        classify=classifier,
        enrich=lambda e, t: t,  # must not be called
    )
    decision = route_failure(
        _mk_event(error_message="ambiguous"),
        deps=deps,
        max_diagnostic_rounds=0,
        use_llm=False,
    )
    assert len(classifier.calls) == 1
    assert decision.route == ROUTE_HUMAN_ESCALATION
    assert decision.resolution == RESOLUTION_UNCLEAR_TIMEOUT


def test_diagnostic_loop_exits_early_when_enrichment_yields_nothing_new():
    """If enrichment returns the SAME telemetry object on the last round,
    the loop breaks without running an extra classify call.  This is the
    fail-loud anti-spin guard.
    """
    same = InfraTelemetry(worker_status="healthy")

    classifier = _ClassifyStub([
        _classification(CLASSIFICATION_UNCLEAR),
        _classification(CLASSIFICATION_UNCLEAR),
    ])

    def enrich(event, existing):
        return same  # same object identity each round

    deps = _make_deps(
        classify=classifier,
        enrich=enrich,
    )
    decision = route_failure(
        _mk_event(error_message="ambiguous"),
        infra_telemetry=same,
        deps=deps,
        max_diagnostic_rounds=1,
        use_llm=False,
    )
    # Initial call (1) — the guard breaks out on the first (and only)
    # loop iteration because enrichment produced no new evidence, so
    # the orchestrator refuses to burn an extra classify call on
    # identical inputs.  Fail-loud: spin-prevention takes precedence.
    assert len(classifier.calls) == 1
    assert decision.route == ROUTE_HUMAN_ESCALATION
    assert decision.resolution == RESOLUTION_UNCLEAR_TIMEOUT


def test_negative_max_diagnostic_rounds_raises():
    deps = _make_deps(classify=_ClassifyStub([_classification(CLASSIFICATION_CONTENT)]))
    with pytest.raises(ValueError, match=">= 0"):
        route_failure(
            _mk_event(qa_verdict="rejected"),
            deps=deps,
            max_diagnostic_rounds=-1,
            use_llm=False,
        )


# ---------------------------------------------------------------------------
# Blackboard writes (output_key pattern)
# ---------------------------------------------------------------------------


def test_blackboard_populated_on_content_route():
    def fake_supervisor(context):
        return EscalationAction(
            action="replace_with_brand_card",
            scene_id="s002",
        )

    deps = _make_deps(
        classify=_ClassifyStub([_classification(CLASSIFICATION_CONTENT)]),
        supervisor=fake_supervisor,
    )
    state: dict = {}
    route_failure(_mk_event(qa_verdict="rejected"), deps=deps, state=state, use_llm=False)

    assert BLACKBOARD_RESULT_KEY in state
    assert BLACKBOARD_SUMMARY_KEY in state
    assert BLACKBOARD_DIAGNOSTIC_TRAIL_KEY in state
    assert state[BLACKBOARD_RESULT_KEY]["route"] == ROUTE_CONTENT_LADDER
    assert (
        state[BLACKBOARD_SUMMARY_KEY]
        == "FAILURE_ROUTE: content -> replace_with_brand_card"
    )


def test_blackboard_populated_on_infra_l4():
    def fake_ladder(event: InfraFailureEvent) -> InfraLadderResult:
        return InfraLadderResult(
            success=False,
            terminal_level=int(RecoveryLevel.HUMAN),
            action=InfraRecoveryAction(
                action_type="escalate_human",
                level=int(RecoveryLevel.HUMAN),
                reason="exhausted",
                escalation_id="esc-9999",
            ),
            state_snapshot={},
            escalation_id="esc-9999",
        )

    deps = _make_deps(
        classify=_ClassifyStub([_classification(CLASSIFICATION_INFRA)]),
        run_infra=fake_ladder,
    )
    state: dict = {}
    route_failure(
        _mk_event(error_message="worker died"),
        deps=deps,
        state=state,
        use_llm=False,
    )
    assert state[BLACKBOARD_RESULT_KEY]["escalation_id"] == "esc-9999"
    assert "human_escalation" in state[BLACKBOARD_SUMMARY_KEY]


# ---------------------------------------------------------------------------
# Invariant guard (_assert_no_silent_downgrade)
# ---------------------------------------------------------------------------


def test_invariant_content_route_with_infra_action_raises():
    bad = OrchestratorDecision(
        resolution=RESOLUTION_CONTENT,
        route=ROUTE_CONTENT_LADDER,
        classification=_classification(CLASSIFICATION_CONTENT).to_dict(),
        creative_action={"action": "regenerate_clip"},
        infra_action={"action_type": "retry_on_healthy_worker"},
    )
    with pytest.raises(FailureOrchestratorError, match="silent downgrade"):
        _assert_no_silent_downgrade(bad)


def test_invariant_infra_route_with_creative_action_raises():
    bad = OrchestratorDecision(
        resolution=RESOLUTION_INFRA,
        route=ROUTE_INFRA_LADDER,
        classification=_classification(CLASSIFICATION_INFRA).to_dict(),
        creative_action={"action": "regenerate_clip"},
        infra_action={"action_type": "retry_on_healthy_worker"},
    )
    with pytest.raises(FailureOrchestratorError, match="silent downgrade"):
        _assert_no_silent_downgrade(bad)


def test_invariant_content_route_with_non_creative_action_raises():
    bad = OrchestratorDecision(
        resolution=RESOLUTION_CONTENT,
        route=ROUTE_CONTENT_LADDER,
        classification=_classification(CLASSIFICATION_CONTENT).to_dict(),
        # "recycle_and_redispatch" is an INFRA action name — if it ever
        # reached the push path it would be the exact silent-downgrade
        # bug this invariant exists to catch.
        creative_action={"action": "recycle_and_redispatch"},
    )
    with pytest.raises(FailureOrchestratorError, match="non-creative"):
        _assert_no_silent_downgrade(bad)


def test_invariant_l4_without_escalation_id_raises():
    bad = OrchestratorDecision(
        resolution=RESOLUTION_UNCLEAR_TIMEOUT,
        route=ROUTE_HUMAN_ESCALATION,
        classification=_classification(CLASSIFICATION_UNCLEAR).to_dict(),
        escalation_id=None,
    )
    with pytest.raises(FailureOrchestratorError, match="escalation_id"):
        _assert_no_silent_downgrade(bad)


# ---------------------------------------------------------------------------
# Failure-signature inference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error, expected",
    [
        ("CUDA out of memory", "oom"),
        ("VRAM exhausted", "vram_exhausted"),
        ("CUDA error: device-side assert", "cuda_error"),
        ("GPU driver reset", "driver_reset"),
        ("spot instance preempt notice", "preemption"),
        ("worker boot failed during cold start", "cold_start_fail"),
        ("Connection refused", "network_partition"),
        ("S3 bucket unreachable", "storage_unreachable"),
        ("401 Unauthorized", "auth_revoked"),
        ("429 rate limit exceeded", "billing_trip"),
        ("process crashed with SIGKILL", "worker_death"),
        ("completely unknown explosion", "worker_death"),
    ],
)
def test_infer_infra_signature_maps_common_errors(error: str, expected: str):
    event = _mk_event(error_message=error)
    assert _infer_infra_signature(event) == expected


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_route_failure_rejects_non_FailureEvent():
    with pytest.raises(TypeError):
        route_failure({"operation_name": "x"})  # type: ignore[arg-type]


def test_route_failure_propagates_classifier_errors():
    """A classifier that raises must not be silently swallowed."""

    def boom(event, telemetry, use_llm, state):
        from agents.diagnostic_classifier import ClassificationError

        raise ClassificationError("LLM offline")

    deps = _make_deps(classify=boom)
    with pytest.raises(Exception, match="LLM offline"):
        route_failure(_mk_event(error_message="anything"), deps=deps, use_llm=False)
