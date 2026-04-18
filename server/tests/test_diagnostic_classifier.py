"""Tests for the diagnostic classifier (ARCH-C1, #140).

Coverage:
- Each classification case (content, infra, unclear).
- Each content signal source (qa_verdict, qa_reason, bad_prompt regex,
  script issue, content-mismatch, contract violation).
- Each infra signal source (OOM, CUDA, process crash, network partition,
  worker unreachable, timeout, gpu_driver, disk_full, telemetry-derived).
- Infra telemetry signals propagate from infra_agent snapshots.
- Confidence thresholds (strong, weak, mixed, no-signal, LLM clamp).
- Fail-loud behaviour: malformed inputs and LLM failures raise rather
  than silently defaulting.
- Blackboard side-effect: ``state[BLACKBOARD_KEY]`` is populated.
- Exposed as a callable for the orchestrator's failure entry point.
"""

from __future__ import annotations

import os
import sys

import pytest

_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from agents.diagnostic_classifier import (  # noqa: E402
    BLACKBOARD_KEY,
    BLACKBOARD_SUMMARY_KEY,
    CLASSIFICATION_CONTENT,
    CLASSIFICATION_INFRA,
    CLASSIFICATION_UNCLEAR,
    HEURISTIC_STRONG_CONFIDENCE,
    HEURISTIC_WEAK_CONFIDENCE,
    MIXED_SIGNAL_CONFIDENCE,
    NO_SIGNAL_CONFIDENCE,
    VALID_CLASSIFICATIONS,
    Classification,
    ClassificationError,
    FailureEvent,
    InfraTelemetry,
    _classifier_before_agent_callback,
    classify_failure,
    set_llm_client_factory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_event(**overrides) -> FailureEvent:
    defaults: dict = {
        "operation_name": "video_generation",
        "error_message": "",
        "exception_type": "",
        "stack_trace": "",
        "worker_id": "",
        "qa_verdict": "",
        "qa_reason": "",
        "pipeline_stage": "production",
    }
    defaults.update(overrides)
    return FailureEvent(**defaults)


class _StubLLM:
    """Deterministic LLM substitute: always returns the same JSON string."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, model: str, system: str, prompt: str) -> str:
        self.calls.append((model, system, prompt))
        return self.response


@pytest.fixture(autouse=True)
def _restore_llm_factory():
    """Ensure each test starts / ends with the default LLM backend."""
    set_llm_client_factory(None)
    yield
    set_llm_client_factory(None)


# ---------------------------------------------------------------------------
# 1. Classification dataclass contract
# ---------------------------------------------------------------------------

def test_classification_dataclass_validates_classification():
    with pytest.raises(ValueError, match="Invalid classification"):
        Classification(classification="other", reasoning="x", confidence=0.5)


def test_classification_dataclass_validates_confidence_range():
    with pytest.raises(ValueError, match="confidence must be in"):
        Classification(
            classification=CLASSIFICATION_INFRA, reasoning="x", confidence=1.5
        )
    with pytest.raises(ValueError, match="confidence must be in"):
        Classification(
            classification=CLASSIFICATION_INFRA, reasoning="x", confidence=-0.1
        )


def test_classification_dataclass_requires_reasoning():
    with pytest.raises(ValueError, match="reasoning"):
        Classification(
            classification=CLASSIFICATION_INFRA, reasoning="", confidence=0.5
        )


def test_classification_to_dict_round_trip():
    c = Classification(
        classification=CLASSIFICATION_CONTENT,
        reasoning="QA rejected clip",
        confidence=0.85,
        signals=["content.qa_verdict"],
        source="heuristic",
    )
    assert c.to_dict() == {
        "classification": CLASSIFICATION_CONTENT,
        "reasoning": "QA rejected clip",
        "confidence": 0.85,
        "signals": ["content.qa_verdict"],
        "source": "heuristic",
    }


def test_valid_classifications_are_the_three_documented():
    assert VALID_CLASSIFICATIONS == frozenset({"content", "infra", "unclear"})


# ---------------------------------------------------------------------------
# 2. Heuristic classification — INFRA signals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("error_text", "expected_signal"),
    [
        ("CUDA out of memory: tried to allocate 12.0 GiB", "infra.oom_error"),
        ("OOM killed on worker", "infra.oom_error"),
        ("CUDA error: invalid device ordinal", "infra.cuda_error"),
        ("CUDA runtime assert at kernel launch", "infra.cuda_error"),
        ("Process crashed with SIGSEGV", "infra.process_crash"),
        ("worker process died unexpectedly, core dumped", "infra.process_crash"),
        ("ConnectionError: Connection refused by peer", "infra.network_partition"),
        ("ECONNRESET from upstream", "infra.network_partition"),
        ("HTTPConnectionPool: Max retries exceeded", "infra.worker_unreachable"),
        (
            "Failed to establish a new connection to gpu-worker",
            "infra.worker_unreachable",
        ),
        ("asyncio.TimeoutError: wait_for exceeded 30s", "infra.timeout"),
        ("socket.timeout on /status", "infra.timeout"),
        ("nvidia-smi failed: NVML driver not loaded", "infra.gpu_driver"),
        ("No space left on device: ENOSPC", "infra.disk_full"),
    ],
)
def test_infra_signal_sources_each_classify_strongly(error_text, expected_signal):
    """Every infra signal source yields strong-infra when no content signal
    is present AND at least one more infra signal fires."""
    event = _mk_event(error_message=error_text)
    # Pair with a telemetry infra signal to push to "multiple" without
    # introducing any content signal.
    telemetry = InfraTelemetry(
        worker_status="unreachable", consecutive_failures=5
    )
    result = classify_failure(event, telemetry, use_llm=False)
    assert result.classification == CLASSIFICATION_INFRA
    assert result.source == "heuristic"
    assert result.confidence == HEURISTIC_STRONG_CONFIDENCE
    assert expected_signal in result.signals


def test_infra_multiple_signals_without_telemetry_classify_strongly():
    """Two infra signals in the error text alone → strong infra."""
    event = _mk_event(
        error_message="CUDA out of memory; subsequent CUDA error on retry",
    )
    result = classify_failure(event, use_llm=False)
    assert result.classification == CLASSIFICATION_INFRA
    assert result.confidence == HEURISTIC_STRONG_CONFIDENCE
    assert "infra.oom_error" in result.signals
    assert "infra.cuda_error" in result.signals


def test_infra_stack_trace_contributes_signal():
    event = _mk_event(
        error_message="Worker returned 500",
        stack_trace=(
            "Traceback (most recent call last):\n"
            '  File "worker.py", line 1, in run\n'
            "torch.cuda.OutOfMemoryError: CUDA out of memory\n"
        ),
    )
    # Only one infra signal from stack, none from message — insufficient
    # for a strong heuristic verdict; should fall through to LLM or unclear.
    result = classify_failure(event, use_llm=False)
    assert "infra.oom_error" in result.signals
    assert result.classification == CLASSIFICATION_UNCLEAR


# ---------------------------------------------------------------------------
# 3. Heuristic classification — CONTENT signals
# ---------------------------------------------------------------------------

def test_qa_verdict_plus_qa_reason_classify_as_content():
    event = _mk_event(
        error_message="Gatekeeper verdict: QA rejected clip",
        qa_verdict="rejected",
        qa_reason=(
            "Visual shows mountains instead of a kitchen setting; "
            "content mismatch with scene description."
        ),
    )
    result = classify_failure(event, use_llm=False)
    assert result.classification == CLASSIFICATION_CONTENT
    assert result.confidence == HEURISTIC_STRONG_CONFIDENCE
    assert "content.qa_verdict" in result.signals
    assert "content.qa_rejected" in result.signals


def test_script_and_contract_signals_classify_as_content():
    event = _mk_event(
        operation_name="scenario_refinement",
        pipeline_stage="scenario",
        error_message=(
            "ContractViolation: narration too long, timing failed — "
            "scene over budget by 3.2s"
        ),
        exception_type="ContractViolation",
    )
    result = classify_failure(event, use_llm=False)
    assert result.classification == CLASSIFICATION_CONTENT
    assert result.confidence == HEURISTIC_STRONG_CONFIDENCE
    assert "content.contract_violation" in result.signals
    assert "content.script_issue" in result.signals


def test_bad_prompt_signal_alone_is_weak_and_unclear_without_llm():
    event = _mk_event(
        error_message="bad prompt detected in scene 3", use_llm=False
    ) if False else _mk_event(error_message="bad prompt detected in scene 3")
    result = classify_failure(event, use_llm=False)
    # One content signal only → heuristic cannot commit → unclear
    assert result.classification == CLASSIFICATION_UNCLEAR
    assert "content.bad_prompt" in result.signals
    assert result.confidence == HEURISTIC_WEAK_CONFIDENCE


def test_content_mismatch_qa_reason_signal():
    event = _mk_event(
        qa_verdict="rejected",
        qa_reason="wrong subject; shows fields instead of kitchen",
    )
    result = classify_failure(event, use_llm=False)
    assert result.classification == CLASSIFICATION_CONTENT
    assert "content.qa_verdict" in result.signals
    assert "content.content_mismatch" in result.signals


# ---------------------------------------------------------------------------
# 4. UNCLEAR cases — mixed / missing signals
# ---------------------------------------------------------------------------

def test_mixed_signals_return_unclear_without_llm():
    """Both an infra and a content signal → heuristic cannot decide → unclear."""
    event = _mk_event(
        error_message=(
            "CUDA out of memory during generation; gatekeeper also "
            "reports QA rejected"
        ),
        qa_verdict="rejected",
    )
    result = classify_failure(event, use_llm=False)
    assert result.classification == CLASSIFICATION_UNCLEAR
    assert result.confidence == MIXED_SIGNAL_CONFIDENCE
    assert "infra.oom_error" in result.signals
    assert "content.qa_verdict" in result.signals


def test_no_signal_returns_unclear_with_low_confidence_without_llm():
    event = _mk_event(
        error_message="generic error with no recognisable keywords here"
    )
    result = classify_failure(event, use_llm=False)
    assert result.classification == CLASSIFICATION_UNCLEAR
    assert result.confidence == NO_SIGNAL_CONFIDENCE
    assert result.signals == []


# ---------------------------------------------------------------------------
# 5. Infra telemetry sourcing — per #125 contract
# ---------------------------------------------------------------------------

def test_telemetry_unreachable_plus_consecutive_failures_is_infra():
    event = _mk_event(error_message="request failed")
    telemetry = InfraTelemetry(
        worker_status="unreachable",
        consecutive_failures=5,
        vm_escalation_severity="critical",
    )
    result = classify_failure(event, telemetry, use_llm=False)
    assert result.classification == CLASSIFICATION_INFRA
    assert "infra.telemetry_worker_unreachable" in result.signals
    assert "infra.telemetry_consecutive_failures" in result.signals
    assert "infra.vm_escalation_critical" in result.signals


def test_telemetry_systemic_patterns_contribute_infra_signals():
    event = _mk_event(error_message="generation failed")
    telemetry = InfraTelemetry(
        worker_status="degraded",
        systemic_patterns=["cascade_failure", "common_error"],
    )
    result = classify_failure(event, telemetry, use_llm=False)
    assert result.classification == CLASSIFICATION_INFRA
    assert "infra.telemetry_worker_degraded" in result.signals
    assert "infra.systemic_cascade_failure" in result.signals
    assert "infra.systemic_common_error" in result.signals


def test_telemetry_model_not_loaded_is_infra_signal():
    event = _mk_event(error_message="inference failed")
    telemetry = InfraTelemetry(
        worker_status="degraded",
        model_loaded=False,
        vm_escalation_severity="warning",
    )
    result = classify_failure(event, telemetry, use_llm=False)
    assert result.classification == CLASSIFICATION_INFRA
    assert "infra.telemetry_model_not_loaded" in result.signals


def test_telemetry_worker_last_error_reparsed_for_infra_keywords():
    event = _mk_event(error_message="job failed")
    telemetry = InfraTelemetry(
        worker_status="degraded",
        worker_last_error="CUDA out of memory during warmup",
    )
    result = classify_failure(event, telemetry, use_llm=False)
    assert result.classification == CLASSIFICATION_INFRA
    assert "infra.oom_error" in result.signals
    assert "infra.telemetry_worker_degraded" in result.signals


# ---------------------------------------------------------------------------
# 6. Input validation — fail loud
# ---------------------------------------------------------------------------

def test_classify_failure_rejects_non_failure_event():
    with pytest.raises(TypeError, match="FailureEvent"):
        classify_failure({"error_message": "x"}, use_llm=False)  # type: ignore[arg-type]


def test_classify_failure_requires_operation_name():
    event = _mk_event(operation_name="", error_message="x")
    with pytest.raises(ValueError, match="operation_name"):
        classify_failure(event, use_llm=False)


def test_classify_failure_requires_some_failure_signal_input():
    event = _mk_event(error_message="", qa_verdict="")
    with pytest.raises(ValueError, match="error_message or qa_verdict"):
        classify_failure(event, use_llm=False)


# ---------------------------------------------------------------------------
# 7. LLM reasoning step
# ---------------------------------------------------------------------------

def test_llm_resolves_mixed_signals_to_infra():
    event = _mk_event(
        error_message=(
            "CUDA out of memory during generation; downstream gatekeeper "
            "then logged 'QA rejected' for the placeholder frame"
        ),
        qa_verdict="rejected",
    )
    stub = _StubLLM(
        '{"classification": "infra", '
        '"reasoning": "Root cause is OOM; QA rejection is downstream noise.",'
        '"confidence": 0.9}'
    )
    set_llm_client_factory(lambda: stub)
    result = classify_failure(event, use_llm=True)
    assert result.classification == CLASSIFICATION_INFRA
    assert result.confidence == pytest.approx(0.9)
    assert result.source == "heuristic+llm"
    assert len(stub.calls) == 1


def test_llm_resolves_single_weak_signal_to_unclear():
    event = _mk_event(error_message="bad prompt detected in scene 3")
    stub = _StubLLM(
        '{"classification": "unclear", '
        '"reasoning": "Only one weak content signal; needs diagnostic loop.",'
        '"confidence": 0.4}'
    )
    set_llm_client_factory(lambda: stub)
    result = classify_failure(event, use_llm=True)
    assert result.classification == CLASSIFICATION_UNCLEAR
    assert result.source == "heuristic+llm"


def test_llm_with_no_heuristic_signals_reports_llm_source():
    event = _mk_event(error_message="opaque error text without keywords")
    stub = _StubLLM(
        '{"classification": "content", '
        '"reasoning": "Operation name implies prompt issue.",'
        '"confidence": 0.7}'
    )
    set_llm_client_factory(lambda: stub)
    result = classify_failure(event, use_llm=True)
    assert result.classification == CLASSIFICATION_CONTENT
    assert result.source == "llm"
    assert result.signals == []


def test_llm_response_with_markdown_fences_parses():
    event = _mk_event(error_message="bad prompt")
    stub = _StubLLM(
        "```json\n"
        '{"classification": "content", "reasoning": "...", "confidence": 0.6}\n'
        "```"
    )
    set_llm_client_factory(lambda: stub)
    result = classify_failure(event, use_llm=True)
    assert result.classification == CLASSIFICATION_CONTENT


def test_llm_empty_response_raises_classification_error():
    event = _mk_event(error_message="bad prompt")
    set_llm_client_factory(lambda: _StubLLM(""))
    with pytest.raises(ClassificationError, match="Empty"):
        classify_failure(event, use_llm=True)


def test_llm_invalid_json_raises_classification_error():
    event = _mk_event(error_message="bad prompt")
    set_llm_client_factory(lambda: _StubLLM("not valid json at all"))
    with pytest.raises(ClassificationError, match="not valid JSON"):
        classify_failure(event, use_llm=True)


def test_llm_response_missing_fields_raises_classification_error():
    event = _mk_event(error_message="bad prompt")
    set_llm_client_factory(
        lambda: _StubLLM('{"classification": "infra"}')
    )
    with pytest.raises(ClassificationError, match="missing required fields"):
        classify_failure(event, use_llm=True)


def test_llm_invalid_classification_value_raises_classification_error():
    event = _mk_event(error_message="bad prompt")
    set_llm_client_factory(
        lambda: _StubLLM(
            '{"classification": "flaky", "reasoning": "x", "confidence": 0.5}'
        )
    )
    with pytest.raises(ClassificationError, match="invalid classification"):
        classify_failure(event, use_llm=True)


def test_llm_confidence_is_clamped_to_unit_interval():
    event = _mk_event(error_message="bad prompt")
    set_llm_client_factory(
        lambda: _StubLLM(
            '{"classification": "infra", "reasoning": "x", "confidence": 2.5}'
        )
    )
    result = classify_failure(event, use_llm=True)
    assert result.confidence == 1.0

    set_llm_client_factory(
        lambda: _StubLLM(
            '{"classification": "infra", "reasoning": "x", "confidence": -0.5}'
        )
    )
    result = classify_failure(event, use_llm=True)
    assert result.confidence == 0.0


def test_llm_strong_heuristic_skips_llm_call():
    """When heuristics commit with STRONG confidence, LLM must not be consulted."""
    event = _mk_event(error_message="CUDA out of memory; CUDA error")
    stub = _StubLLM('{"classification": "unclear", "reasoning": "x", "confidence": 0.1}')
    set_llm_client_factory(lambda: stub)
    result = classify_failure(event, use_llm=True)
    assert result.classification == CLASSIFICATION_INFRA
    assert result.source == "heuristic"
    assert stub.calls == [], "LLM must not be called when heuristics are strong"


# ---------------------------------------------------------------------------
# 8. Blackboard side-effect — state[BLACKBOARD_KEY]
# ---------------------------------------------------------------------------

def test_classify_failure_writes_blackboard_state():
    event = _mk_event(qa_verdict="rejected", qa_reason="wrong subject")
    state: dict = {}
    result = classify_failure(event, use_llm=False, state=state)
    assert BLACKBOARD_KEY in state
    assert state[BLACKBOARD_KEY]["classification"] == result.classification
    assert state[BLACKBOARD_KEY]["reasoning"] == result.reasoning
    assert state[BLACKBOARD_KEY]["confidence"] == result.confidence
    assert state[BLACKBOARD_KEY]["signals"] == list(result.signals)


def test_classify_failure_without_state_is_pure():
    event = _mk_event(qa_verdict="rejected", qa_reason="wrong subject")
    # No state arg — function must not crash; result is returned normally.
    result = classify_failure(event, use_llm=False)
    assert result.classification == CLASSIFICATION_CONTENT


# ---------------------------------------------------------------------------
# 9. ADK agent callback — contract tests
# ---------------------------------------------------------------------------

class _FakeCallbackContext:
    """Minimal stand-in for google.adk CallbackContext used in tests."""

    def __init__(self, state: dict) -> None:
        self.state = state


def test_agent_callback_requires_failure_event_on_state():
    ctx = _FakeCallbackContext(state={})
    with pytest.raises(ClassificationError, match="no 'failure_event'"):
        _classifier_before_agent_callback(ctx)


def test_agent_callback_accepts_failure_event_as_dict():
    state = {
        "failure_event": {
            "operation_name": "video_generation",
            "error_message": "CUDA out of memory; CUDA error during warmup",
            "pipeline_stage": "production",
        }
    }
    ctx = _FakeCallbackContext(state=state)
    content = _classifier_before_agent_callback(ctx)
    # Blackboard structured classification is written.
    assert state[BLACKBOARD_KEY]["classification"] == CLASSIFICATION_INFRA
    # Content text (summary) returned so ADK skips the LLM.
    assert content is not None
    text = content.parts[0].text
    assert "DIAGNOSTIC_CLASSIFICATION" in text
    assert "infra" in text


def test_agent_callback_accepts_failure_event_as_dataclass():
    event = _mk_event(
        error_message="QA rejected: wrong subject; content mismatch"
    )
    state = {"failure_event": event}
    ctx = _FakeCallbackContext(state=state)
    _classifier_before_agent_callback(ctx)
    assert state[BLACKBOARD_KEY]["classification"] == CLASSIFICATION_CONTENT


def test_agent_callback_rejects_invalid_failure_event_type():
    ctx = _FakeCallbackContext(state={"failure_event": 42})
    with pytest.raises(ClassificationError, match="must be FailureEvent or dict"):
        _classifier_before_agent_callback(ctx)


def test_agent_callback_accepts_telemetry_as_dict():
    state = {
        "failure_event": {
            "operation_name": "video_generation",
            "error_message": "worker returned 503",
            "pipeline_stage": "production",
        },
        "infra_telemetry": {
            "worker_status": "unreachable",
            "consecutive_failures": 6,
            "vm_escalation_severity": "critical",
        },
    }
    ctx = _FakeCallbackContext(state=state)
    _classifier_before_agent_callback(ctx)
    assert state[BLACKBOARD_KEY]["classification"] == CLASSIFICATION_INFRA


def test_agent_callback_rejects_invalid_telemetry_type():
    state = {
        "failure_event": {
            "operation_name": "video_generation",
            "error_message": "x",
        },
        "infra_telemetry": "not-a-dict",
    }
    ctx = _FakeCallbackContext(state=state)
    with pytest.raises(ClassificationError, match="InfraTelemetry or dict"):
        _classifier_before_agent_callback(ctx)


# ---------------------------------------------------------------------------
# 10. Orchestrator integration point — the callable is importable and
#     returns a dict-serialisable result.
# ---------------------------------------------------------------------------

def test_classify_failure_is_importable_from_package_root():
    # The orchestrator will import the callable as:
    #   from agents.diagnostic_classifier import classify_failure
    import importlib

    mod = importlib.import_module("agents.diagnostic_classifier")
    assert hasattr(mod, "classify_failure")
    assert callable(mod.classify_failure)
    # The dataclass-returning API is stable across content / infra / unclear.
    e = FailureEvent(
        operation_name="video_generation",
        error_message="CUDA out of memory; CUDA error",
    )
    result = mod.classify_failure(e, use_llm=False)
    assert result.to_dict()["classification"] in VALID_CLASSIFICATIONS


def test_blackboard_summary_key_is_distinct():
    """The summary (output_key) and structured (BLACKBOARD_KEY) slots must
    not collide — otherwise the text output would overwrite the dict."""
    assert BLACKBOARD_KEY != BLACKBOARD_SUMMARY_KEY
