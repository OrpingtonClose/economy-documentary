"""
Diagnostic classifier — content vs infra vs unclear.

Implements ARCH-C1 (#140), parent workstream ARCH-C (#125), meta ARCH-2026
(#122, invariant #5 "dual-axis failure classification").

Given a failure event (plus optional infra telemetry sourced from
``server/infra_agent.py`` / ``server/fleet/systemic_detector.py``) the
classifier returns one of:

    content — artifact-level failure (bad prompt, bad script, QA verdict
              rejecting the output, content-shape violation).  Routes to
              the existing content recovery ladder in ``server/recovery.py``.
    infra   — runtime failure (OOM, CUDA error, process crash, network
              partition, worker unreachable as reported by infra_agent
              telemetry).  Routes to the infra ladder (ARCH-C2, #141).
    unclear — signals are ambiguous or contradictory.  Caller runs a short
              diagnostic loop before retrying classification.

Architecture invariants (per #122 DoD):

- ADK ``Agent`` subclass exposed as ``diagnostic_classifier_agent`` so the
  orchestrator (ARCH-C3, #142) can compose it via the normal ADK flow.
- Cross-stage state flows via the blackboard — ``classify_failure`` writes
  the result into ``state[BLACKBOARD_KEY]`` so downstream recovery ladders
  can pick it up without a side channel.
- Fail loud: no silent default classification.  When the LLM reasoning
  step is required and fails, ``ClassificationError`` is raised so the
  caller must make an explicit decision.

Scope of this module (ARCH-C1 only): the classifier itself plus a clean
callable entry point.  Wiring into the production orchestrator's failure
path is explicitly deferred to ARCH-C3 (#142).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public return types
# ---------------------------------------------------------------------------

CLASSIFICATION_CONTENT = "content"
CLASSIFICATION_INFRA = "infra"
CLASSIFICATION_UNCLEAR = "unclear"

VALID_CLASSIFICATIONS = frozenset(
    {CLASSIFICATION_CONTENT, CLASSIFICATION_INFRA, CLASSIFICATION_UNCLEAR}
)

BLACKBOARD_KEY = "diagnostic_classification"
"""Blackboard state key the classifier writes its result under.

Orchestrators (ARCH-C3, #142) read ``state[BLACKBOARD_KEY]`` to dispatch
to the correct recovery ladder.  The value is a ``dict`` produced by
``Classification.to_dict()`` — never a bare string.
"""

BLACKBOARD_SUMMARY_KEY = "diagnostic_classification_summary"
"""Secondary blackboard key — holds the short human-readable summary
text emitted by the ADK agent (written via ``output_key``)."""


class ClassificationError(RuntimeError):
    """Raised when the classifier cannot produce a valid result.

    Fail-loud contract: we never silently default to ``content`` or
    ``infra``.  ``unclear`` is a legitimate classification (the caller is
    expected to run a short diagnostic loop), but it is only emitted when
    the heuristic / LLM path explicitly concluded ambiguity — not as a
    fallback for an LLM-backend error.  When the LLM reasoning step is
    invoked but fails, this error is raised so the caller must take an
    explicit action.
    """


@dataclass(frozen=True)
class Classification:
    """Result returned by the diagnostic classifier."""

    classification: str
    """One of ``"content"``, ``"infra"``, ``"unclear"``."""

    reasoning: str
    """Human-readable explanation of the decision."""

    confidence: float
    """Confidence in the classification in [0.0, 1.0]."""

    signals: list[str] = field(default_factory=list)
    """Names of signals that fired during heuristic analysis
    (e.g. ``"infra.oom_error"``, ``"content.qa_verdict"``)."""

    source: str = "heuristic"
    """Which stage produced the result: ``"heuristic"`` | ``"llm"`` |
    ``"heuristic+llm"``."""

    def __post_init__(self) -> None:
        if self.classification not in VALID_CLASSIFICATIONS:
            raise ValueError(
                f"Invalid classification {self.classification!r}; "
                f"must be one of {sorted(VALID_CLASSIFICATIONS)}"
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"confidence must be in [0.0, 1.0]; got {self.confidence!r}"
            )
        if not self.reasoning:
            raise ValueError("reasoning must be a non-empty string")

    def to_dict(self) -> dict:
        return {
            "classification": self.classification,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "signals": list(self.signals),
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Input types
# ---------------------------------------------------------------------------

@dataclass
class FailureEvent:
    """Failure event handed to the classifier at the orchestrator's
    failure entry point.

    Only ``operation_name`` is strictly required; at least one of
    ``error_message`` / ``qa_verdict`` must carry signal for classification
    to proceed (``classify_failure`` raises ``ValueError`` otherwise).
    """

    operation_name: str
    """e.g. ``"video_generation"``, ``"tts"``, ``"assembly"``."""

    error_message: str = ""
    """Exception message or otherwise summarised failure description."""

    exception_type: str = ""
    """Class name of the raised exception if available
    (e.g. ``"RuntimeError"``, ``"TimeoutError"``)."""

    stack_trace: str = ""
    """Optional stack trace — keywords within are inspected by heuristics."""

    worker_id: str = ""
    """Identifier for the GPU/TTS worker, if the failure originated there."""

    qa_verdict: str = ""
    """Gatekeeper verdict, if the failure came from QA
    (e.g. ``"rejected"``, ``"poor"``)."""

    qa_reason: str = ""
    """QA-provided rationale (bad-prompt hints, missing subject, etc.)."""

    pipeline_stage: str = ""
    """Stage where the failure occurred (``"scenario"``, ``"audio"``,
    ``"production"``, ``"assembly"``, ...)."""


@dataclass
class InfraTelemetry:
    """Infra-agent telemetry bundled with the failure event.

    Worker health is inferred **only** from signals sourced here — per
    #125 the classifier must not treat raw job-outcome error messages as
    evidence of worker health.
    """

    worker_status: str = ""
    """``"healthy"`` | ``"degraded"`` | ``"unreachable"`` | ``"unknown"`` | ``""``."""

    worker_last_error: str = ""
    """InfraAgent's last observed worker-level error message, if any."""

    consecutive_failures: int = 0
    """Consecutive failed ``/status`` polls for this worker."""

    systemic_patterns: list[str] = field(default_factory=list)
    """Pattern types from ``SystemicDetector`` (e.g. ``"cascade_failure"``,
    ``"common_error"``, ``"performance_degradation"``)."""

    vm_escalation_severity: str = ""
    """Max severity of recent VM-agent escalations:
    ``"info"`` | ``"warning"`` | ``"critical"`` | ``""``."""

    model_loaded: bool = True
    """Whether the worker's model was reported loaded at last poll."""


# ---------------------------------------------------------------------------
# Heuristic signal matchers
# ---------------------------------------------------------------------------

# Infra keyword patterns — each regex matched case-insensitively.
_INFRA_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "oom_error",
        re.compile(r"\b(?:cuda\s+)?out\s+of\s+memory\b|\bOOM\b", re.I),
    ),
    (
        "cuda_error",
        re.compile(
            r"\bCUDA\s*(?:error|runtime|driver|assert|failure|kernel)\b",
            re.I,
        ),
    ),
    (
        "process_crash",
        re.compile(
            r"\b(?:segfault|segmentation\s+fault|SIGKILL|SIGTERM|SIGSEGV|"
            r"process\s+(?:crashed|killed|died|exited\s+unexpectedly)|"
            r"core\s+dumped|abnormal\s+exit|worker\s+process\s+died)\b",
            re.I,
        ),
    ),
    (
        "network_partition",
        re.compile(
            r"\b(?:connection\s+(?:refused|reset|aborted)|"
            r"network\s+(?:partition|unreachable)|ECONNREFUSED|ECONNRESET|"
            r"ETIMEDOUT|EHOSTUNREACH|no\s+route\s+to\s+host)\b",
            re.I,
        ),
    ),
    (
        "worker_unreachable",
        re.compile(
            r"\b(?:worker\s+unreachable|worker\s+down|/status\s+(?:timed\s*out|failed)|"
            r"URLError|HTTPConnectionPool|"
            r"failed\s+to\s+establish\s+a\s+new\s+connection|"
            r"unable\s+to\s+connect\s+to\s+worker)\b",
            re.I,
        ),
    ),
    (
        "timeout",
        re.compile(
            r"\b(?:timed\s*out|timeout\s+(?:expired|exceeded)|TimeoutError|"
            r"ReadTimeoutError|asyncio\.TimeoutError|socket\.timeout)\b",
            re.I,
        ),
    ),
    (
        "gpu_driver",
        re.compile(
            r"\b(?:nvidia-smi|NVML|NCCL|cuDNN|GPU\s+driver)\b[^\n]{0,60}"
            r"(?:error|failed|not\s+found|missing|unavailable)",
            re.I,
        ),
    ),
    (
        "disk_full",
        re.compile(r"\b(?:no\s+space\s+left|disk\s+full|ENOSPC)\b", re.I),
    ),
]

# Content keyword patterns.
_CONTENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "qa_rejected",
        re.compile(
            r"\b(?:QA[_\s]+(?:rejected|reject|hints?)|"
            r"quality[:\s]+(?:rejected|poor)|QA_HINTS|"
            r"gatekeeper\s+rejected|qa\s+verdict)\b",
            re.I,
        ),
    ),
    (
        "bad_prompt",
        re.compile(
            r"\b(?:bad\s+prompt|prompt\s+(?:rejected|invalid|malformed|too\s+short|empty)|"
            r"unsafe\s+prompt|prompt\s+failed\s+policy)\b",
            re.I,
        ),
    ),
    (
        "script_issue",
        re.compile(
            r"\b(?:rhetorical\s+question|ADHD\s+(?:violation|compliance)|"
            r"script\s+(?:invalid|malformed)|"
            r"narration\s+(?:too\s+long|too\s+short|over\s+budget|under\s+budget)|"
            r"timing[_\s]+(?:failed|analysis)|scene\s+(?:missing|invalid)|"
            r"missing\s+(?:pronunciation_hints|hook_spec|outro_spec|ssml))\b",
            re.I,
        ),
    ),
    (
        "content_mismatch",
        re.compile(
            r"\b(?:wrong\s+subject|off[- ]topic|shows\s+[a-z ]+\s+instead\s+of|"
            r"missing\s+(?:subject|setting)|mismatch(?:ed)?\s+(?:content|subject)|"
            r"visual\s+does\s+not\s+match)\b",
            re.I,
        ),
    ),
    (
        "contract_violation",
        re.compile(
            r"\b(?:ContractViolation|contract\s+violat|OTIO\s+(?:invalid|violation)|"
            r"timeline\s+(?:guardian|invariant))\b",
            re.I,
        ),
    ),
]


def _match_patterns(
    text: str,
    patterns: list[tuple[str, re.Pattern[str]]],
    prefix: str,
) -> list[str]:
    """Return prefixed signal names for every pattern that matches ``text``."""
    if not text:
        return []
    hits: list[str] = []
    for name, regex in patterns:
        if regex.search(text):
            hits.append(f"{prefix}.{name}")
    return hits


@dataclass
class _HeuristicAnalysis:
    infra_signals: list[str]
    content_signals: list[str]
    telemetry_signals: list[str]
    all_signals: list[str]


def _run_heuristics(
    event: FailureEvent,
    telemetry: Optional[InfraTelemetry],
) -> _HeuristicAnalysis:
    # Combine free-text fields the keyword regexes inspect.
    haystack = "\n".join(
        part
        for part in (event.error_message, event.exception_type, event.stack_trace)
        if part
    )

    infra_signals = _match_patterns(haystack, _INFRA_PATTERNS, "infra")
    content_signals = _match_patterns(haystack, _CONTENT_PATTERNS, "content")

    # QA verdict is a strong content signal on its own.
    if event.qa_verdict and event.qa_verdict.lower() in {"rejected", "poor", "reject"}:
        sig = "content.qa_verdict"
        if sig not in content_signals:
            content_signals.append(sig)

    if event.qa_reason:
        for extra in _match_patterns(event.qa_reason, _CONTENT_PATTERNS, "content"):
            if extra not in content_signals:
                content_signals.append(extra)

    # Infra telemetry signals — inferred ONLY from infra_agent data
    # (contract with #125: worker health never inferred from job outcomes).
    telemetry_signals: list[str] = []
    if telemetry is not None:
        status = (telemetry.worker_status or "").lower()
        if status == "unreachable":
            telemetry_signals.append("infra.telemetry_worker_unreachable")
        elif status == "degraded":
            telemetry_signals.append("infra.telemetry_worker_degraded")
        if telemetry.consecutive_failures >= 3:
            telemetry_signals.append("infra.telemetry_consecutive_failures")
        for p in telemetry.systemic_patterns:
            telemetry_signals.append(f"infra.systemic_{p}")
        severity = (telemetry.vm_escalation_severity or "").lower()
        if severity == "critical":
            telemetry_signals.append("infra.vm_escalation_critical")
        elif severity == "warning":
            telemetry_signals.append("infra.vm_escalation_warning")
        if telemetry.model_loaded is False:
            telemetry_signals.append("infra.telemetry_model_not_loaded")
        if telemetry.worker_last_error:
            for extra in _match_patterns(
                telemetry.worker_last_error, _INFRA_PATTERNS, "infra"
            ):
                if extra not in telemetry_signals:
                    telemetry_signals.append(extra)

    # Telemetry signals count as infra evidence for classification weighting.
    combined_infra: list[str] = []
    seen: set[str] = set()
    for s in (*infra_signals, *telemetry_signals):
        if s not in seen:
            combined_infra.append(s)
            seen.add(s)

    all_signals = [*combined_infra, *content_signals]
    return _HeuristicAnalysis(
        infra_signals=combined_infra,
        content_signals=content_signals,
        telemetry_signals=telemetry_signals,
        all_signals=all_signals,
    )


# Confidence thresholds.
HEURISTIC_STRONG_CONFIDENCE = 0.85
"""Multiple signals on one axis, none on the other."""

HEURISTIC_WEAK_CONFIDENCE = 0.55
"""Single signal on one axis — the LLM gets to confirm."""

MIXED_SIGNAL_CONFIDENCE = 0.5
"""Both axes fire — classification is unclear."""

NO_SIGNAL_CONFIDENCE = 0.2
"""No signals matched at all."""


def _heuristic_classify(analysis: _HeuristicAnalysis) -> Optional[Classification]:
    """Classify based on heuristic signals alone.

    Returns ``None`` when the heuristic pass is not confident enough —
    the caller then escalates to the LLM reasoning step (or returns an
    explicit ``unclear`` when ``use_llm=False``).
    """
    n_infra = len(analysis.infra_signals)
    n_content = len(analysis.content_signals)

    # Both axes fired — let the LLM break the tie; never silently pick one.
    if n_infra and n_content:
        return None

    if n_infra >= 2 and n_content == 0:
        return Classification(
            classification=CLASSIFICATION_INFRA,
            reasoning=(
                "Multiple infra signals detected with no content signals: "
                + ", ".join(analysis.infra_signals)
            ),
            confidence=HEURISTIC_STRONG_CONFIDENCE,
            signals=list(analysis.all_signals),
            source="heuristic",
        )

    if n_content >= 2 and n_infra == 0:
        return Classification(
            classification=CLASSIFICATION_CONTENT,
            reasoning=(
                "Multiple content signals detected with no infra signals: "
                + ", ".join(analysis.content_signals)
            ),
            confidence=HEURISTIC_STRONG_CONFIDENCE,
            signals=list(analysis.all_signals),
            source="heuristic",
        )

    # Single weak signal — defer to the LLM reasoning step.
    return None


# ---------------------------------------------------------------------------
# LLM reasoning step
# ---------------------------------------------------------------------------

_CLASSIFIER_MODEL = os.environ.get(
    "DIAGNOSTIC_CLASSIFIER_MODEL", "gemini-2.5-flash"
)

_CLASSIFIER_SYSTEM_INSTRUCTION = (
    "You are the Diagnostic Classifier for a documentary production pipeline.\n"
    "Given a failure event plus optional infra telemetry, classify the "
    "failure as EXACTLY ONE of:\n"
    "  - content: the failure is about the artifact itself — bad prompt, bad "
    "script, QA verdict rejecting the output, content-shape violation. "
    "Content failures route to the content recovery ladder.\n"
    "  - infra: the failure is about the runtime — OOM, CUDA error, process "
    "crash, network partition, worker unreachable as reported by the "
    "infra_agent telemetry. Infra failures route to the infra recovery "
    "ladder.\n"
    "  - unclear: the signals are ambiguous or contradictory. The caller "
    "will run a short diagnostic loop before retrying classification.\n\n"
    "RULES:\n"
    "- Worker health is inferred ONLY from infra_agent telemetry, never "
    "from job-outcome error text.\n"
    "- If both content AND infra telemetry signals fired, pick 'unclear' "
    "unless one side is clearly dominant.\n"
    "- Never invent a classification. If there is no basis, pick 'unclear'.\n"
    "- Respond with a single JSON object. No markdown. No prose outside "
    "the JSON. Schema: {\"classification\": \"content\"|\"infra\"|\"unclear\""
    ", \"reasoning\": string, \"confidence\": number in [0.0, 1.0]}."
)

_CLASSIFIER_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": list(sorted(VALID_CLASSIFICATIONS)),
        },
        "reasoning": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["classification", "reasoning", "confidence"],
}


LLMCallable = Callable[[str, str, str], str]
"""``(model, system_instruction, user_prompt) -> raw JSON text``."""


_llm_client_factory: Optional[Callable[[], LLMCallable]] = None


def set_llm_client_factory(
    factory: Optional[Callable[[], LLMCallable]],
) -> None:
    """Inject a fake LLM client factory for tests.

    The factory is zero-arg and must return a callable with signature
    ``(model, system, prompt) -> str``.  Pass ``None`` to restore the
    default google-genai backend.
    """
    global _llm_client_factory
    _llm_client_factory = factory


def _default_llm_call(model: str, system: str, prompt: str) -> str:
    """Default LLM backend — google-genai with structured output.

    Mirrors the pattern used by
    ``server/agents/production_supervisor.py::_default_llm_call``:
    ``response_mime_type="application/json"`` plus ``response_schema``
    force the model to return the three-field document.
    """
    from google import genai
    from google.genai import types as genai_types

    api_key = (
        os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_GENAI_API_KEY")
    )
    if not api_key:
        raise ClassificationError(
            "Diagnostic classifier LLM step requires GOOGLE_API_KEY / "
            "GEMINI_API_KEY / GOOGLE_GENAI_API_KEY.  Refusing to silently "
            "default the classification."
        )
    client = genai.Client(api_key=api_key)
    config = genai_types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=_CLASSIFIER_RESPONSE_SCHEMA,
        temperature=0.1,
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=config,
    )
    return response.text or ""


def _build_llm_prompt(
    event: FailureEvent,
    telemetry: Optional[InfraTelemetry],
    analysis: _HeuristicAnalysis,
) -> str:
    payload = {
        "failure_event": {
            "operation_name": event.operation_name,
            "pipeline_stage": event.pipeline_stage,
            "exception_type": event.exception_type,
            "error_message": event.error_message[:4000],
            "stack_trace": event.stack_trace[:2000],
            "worker_id": event.worker_id,
            "qa_verdict": event.qa_verdict,
            "qa_reason": event.qa_reason[:2000],
        },
        "infra_telemetry": asdict(telemetry) if telemetry is not None else None,
        "heuristic_signals": {
            "infra_signals": analysis.infra_signals,
            "content_signals": analysis.content_signals,
        },
    }
    return (
        "FAILURE CONTEXT:\n"
        f"{json.dumps(payload, indent=2, default=str)}\n\n"
        "Classify this failure and respond with the JSON object."
    )


def _parse_llm_response(text: str) -> tuple[str, str, float]:
    if not text:
        raise ClassificationError("Empty classifier LLM response")
    stripped = text.strip()
    # Strip markdown fences if the model wrapped the JSON.
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        # Last-ditch extract of first JSON object.
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                raise ClassificationError(
                    f"Classifier response is not valid JSON: {exc}"
                ) from exc
        else:
            raise ClassificationError(
                f"Classifier response is not valid JSON: {exc}"
            ) from exc
    if not isinstance(data, dict):
        raise ClassificationError(
            f"Expected JSON object, got {type(data).__name__}"
        )
    try:
        classification = str(data["classification"]).lower()
        reasoning = str(data["reasoning"])
        confidence = float(data["confidence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ClassificationError(
            f"Classifier response missing required fields: {exc}"
        ) from exc
    if classification not in VALID_CLASSIFICATIONS:
        raise ClassificationError(
            f"Classifier returned invalid classification {classification!r}; "
            f"must be one of {sorted(VALID_CLASSIFICATIONS)}"
        )
    confidence = max(0.0, min(1.0, confidence))
    if not reasoning:
        reasoning = f"Classifier returned {classification} with no rationale."
    return classification, reasoning, confidence


def _llm_classify(
    event: FailureEvent,
    telemetry: Optional[InfraTelemetry],
    analysis: _HeuristicAnalysis,
) -> Classification:
    if _llm_client_factory is not None:
        call = _llm_client_factory()
    else:
        call = _default_llm_call
    prompt = _build_llm_prompt(event, telemetry, analysis)
    raw = call(
        _CLASSIFIER_MODEL, _CLASSIFIER_SYSTEM_INSTRUCTION, prompt
    )
    classification, reasoning, confidence = _parse_llm_response(raw)
    source = "heuristic+llm" if analysis.all_signals else "llm"
    return Classification(
        classification=classification,
        reasoning=reasoning,
        confidence=confidence,
        signals=list(analysis.all_signals),
        source=source,
    )


# ---------------------------------------------------------------------------
# Public callable — the orchestrator failure entry point
# ---------------------------------------------------------------------------

def classify_failure(
    event: FailureEvent,
    infra_telemetry: Optional[InfraTelemetry] = None,
    *,
    use_llm: bool = True,
    state: Optional[dict] = None,
) -> Classification:
    """Classify a pipeline failure as content / infra / unclear.

    The single entry point the production orchestrator (ARCH-C3, #142)
    should call at the failure entry point.  The function is pure with
    respect to its inputs except for one side effect: when ``state`` is
    provided, the classification dict is stored under ``BLACKBOARD_KEY``
    so downstream recovery agents can read it via the blackboard pattern.

    Parameters
    ----------
    event:
        The failure event.  ``operation_name`` must be non-empty and at
        least one of ``error_message`` / ``qa_verdict`` must carry signal.
    infra_telemetry:
        Optional snapshot from ``server/infra_agent.py`` /
        ``server/fleet/systemic_detector.py``.  Per #125, worker health
        is inferred only from infra telemetry, never from job outcomes.
    use_llm:
        When ``True`` (default), ambiguous heuristic results are resolved
        by the LLM reasoning step.  When ``False``, ambiguous results
        return an ``unclear`` classification directly — useful for tests
        and for environments without an LLM backend.
    state:
        Optional ADK-blackboard dict (``callback_context.state``).  When
        supplied, ``state[BLACKBOARD_KEY]`` is set to ``result.to_dict()``.

    Raises
    ------
    TypeError
        If ``event`` is not a ``FailureEvent``.
    ValueError
        If ``event.operation_name`` is empty or both ``error_message``
        and ``qa_verdict`` are empty (nothing to classify).
    ClassificationError
        If the LLM reasoning step is invoked and fails.  Fail-loud: no
        silent default classification is emitted.
    """
    if not isinstance(event, FailureEvent):
        raise TypeError(
            f"event must be FailureEvent, got {type(event).__name__}"
        )
    if not event.operation_name:
        raise ValueError("FailureEvent.operation_name must be non-empty")
    if not event.error_message and not event.qa_verdict:
        raise ValueError(
            "FailureEvent must provide error_message or qa_verdict — "
            "cannot classify an empty failure"
        )

    analysis = _run_heuristics(event, infra_telemetry)
    heuristic_result = _heuristic_classify(analysis)

    if heuristic_result is not None:
        result = heuristic_result
    else:
        both_axes = bool(analysis.infra_signals) and bool(analysis.content_signals)
        if use_llm:
            try:
                result = _llm_classify(event, infra_telemetry, analysis)
            except ClassificationError:
                raise
            except Exception as exc:  # pragma: no cover — defensive
                raise ClassificationError(
                    f"Diagnostic classifier LLM step failed: {exc}"
                ) from exc
        else:
            if both_axes:
                confidence = MIXED_SIGNAL_CONFIDENCE
                reasoning = (
                    "Both infra and content signals fired; caller must run "
                    "a diagnostic loop before classification is possible."
                )
            elif analysis.all_signals:
                confidence = HEURISTIC_WEAK_CONFIDENCE
                reasoning = (
                    "Only weak single-axis signal(s); LLM reasoning is "
                    "disabled so result is unclear."
                )
            else:
                confidence = NO_SIGNAL_CONFIDENCE
                reasoning = (
                    "No signals matched; unable to classify without LLM step."
                )
            result = Classification(
                classification=CLASSIFICATION_UNCLEAR,
                reasoning=reasoning,
                confidence=confidence,
                signals=list(analysis.all_signals),
                source="heuristic",
            )

    if state is not None:
        state[BLACKBOARD_KEY] = result.to_dict()

    logger.info(
        "DiagnosticClassifier: operation=%s stage=%s -> %s "
        "(conf=%.2f, source=%s, signals=%d)",
        event.operation_name,
        event.pipeline_stage,
        result.classification,
        result.confidence,
        result.source,
        len(result.signals),
    )
    return result


# ---------------------------------------------------------------------------
# ADK Agent wrapper — composed via the normal SequentialAgent flow
# ---------------------------------------------------------------------------

_CLASSIFIER_AGENT_INSTRUCTION = """\
You are the Diagnostic Classifier. Classification is handled deterministically
by ``classify_failure`` (invoked from the before-agent callback). Your role is
only to emit the short summary written to the blackboard.
"""


def _classifier_before_agent_callback(callback_context: Any) -> Any:
    """ADK ``before_agent_callback`` wrapper.

    Contract: the caller must stage ``state["failure_event"]`` (either a
    ``FailureEvent`` dataclass or a dict with matching keys) and may
    stage ``state["infra_telemetry"]`` similarly.  The callback runs
    ``classify_failure``, writes the structured result to
    ``state[BLACKBOARD_KEY]``, and returns a ``Content`` so the wrapped
    LLM call is skipped (mirroring the Timeline Guardian pattern).  The
    returned summary text is captured via ``output_key``.
    """
    from google.genai import types as genai_types

    state = callback_context.state
    raw_event = state.get("failure_event") if hasattr(state, "get") else None
    if raw_event is None:
        raise ClassificationError(
            "Diagnostic classifier invoked with no 'failure_event' on the "
            "blackboard — caller contract violated."
        )
    if isinstance(raw_event, FailureEvent):
        event = raw_event
    elif isinstance(raw_event, dict):
        event = FailureEvent(**raw_event)
    else:
        raise ClassificationError(
            "state['failure_event'] must be FailureEvent or dict, got "
            f"{type(raw_event).__name__}"
        )

    raw_tel = state.get("infra_telemetry") if hasattr(state, "get") else None
    telemetry: Optional[InfraTelemetry]
    if raw_tel is None:
        telemetry = None
    elif isinstance(raw_tel, InfraTelemetry):
        telemetry = raw_tel
    elif isinstance(raw_tel, dict):
        telemetry = InfraTelemetry(**raw_tel)
    else:
        raise ClassificationError(
            "state['infra_telemetry'] must be InfraTelemetry or dict, got "
            f"{type(raw_tel).__name__}"
        )

    result = classify_failure(event, telemetry, state=state)
    summary = (
        f"DIAGNOSTIC_CLASSIFICATION: {result.classification} "
        f"(confidence={result.confidence:.2f}, source={result.source}) — "
        f"{result.reasoning[:400]}"
    )

    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(text=summary)],
    )


def _build_diagnostic_classifier_agent():
    """Build the ADK ``Agent`` wrapper for the diagnostic classifier.

    Returns ``None`` when ADK / model-config cannot be imported (e.g.
    minimal CI jobs that only exercise ``classify_failure`` directly).
    """
    try:
        from google.adk.agents import Agent

        from agents.model_config import build_model
    except Exception as exc:
        logger.warning(
            "ADK unavailable (%s) — diagnostic_classifier_agent will be "
            "None; classify_failure() still works as a pure-Python entrypoint.",
            exc,
        )
        return None

    return Agent(
        name="diagnostic_classifier",
        model=build_model(synthesis=True),
        instruction=_CLASSIFIER_AGENT_INSTRUCTION,
        tools=[],
        # ``output_key`` captures the human-readable summary emitted by
        # the callback.  The structured classification dict is written
        # separately to ``state[BLACKBOARD_KEY]`` so downstream consumers
        # (ARCH-C3, #142) can dispatch without parsing text.
        output_key=BLACKBOARD_SUMMARY_KEY,
        before_agent_callback=_classifier_before_agent_callback,
    )


diagnostic_classifier_agent = _build_diagnostic_classifier_agent()


__all__ = [
    "BLACKBOARD_KEY",
    "BLACKBOARD_SUMMARY_KEY",
    "CLASSIFICATION_CONTENT",
    "CLASSIFICATION_INFRA",
    "CLASSIFICATION_UNCLEAR",
    "VALID_CLASSIFICATIONS",
    "Classification",
    "ClassificationError",
    "FailureEvent",
    "HEURISTIC_STRONG_CONFIDENCE",
    "HEURISTIC_WEAK_CONFIDENCE",
    "InfraTelemetry",
    "LLMCallable",
    "MIXED_SIGNAL_CONFIDENCE",
    "NO_SIGNAL_CONFIDENCE",
    "classify_failure",
    "diagnostic_classifier_agent",
    "set_llm_client_factory",
]
