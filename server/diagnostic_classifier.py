"""Signal-based diagnostic classifier — content vs infra vs unclear.

Implements the classification layer that sits between raw failure events and
the dual-axis escalation system.  This module determines whether a failure
consumes **content budget** (media-specific ladder) or **infra budget**
(substrate ladder), preventing content budget from being drained by
substrate issues like OOM, CUDA errors, or network partitions.

Key design principle (Diagram 8 of ``docs/ARCHITECTURE_DIAGRAMS.md``):

    "Workers are inferred from infra observation, never from job outcomes —
    a single failed clip does not condemn a worker, and a single good clip
    does not exonerate one."

This module enforces that principle through two mechanisms:

1. **Classification requires 2+ signals on one axis** before committing to
   CONTENT or INFRA.  A single signal is insufficient — it produces UNCLEAR.
2. **Worker condemnation requires 2+ INDEPENDENT infra signals** (e.g. a
   job_result failure AND an infra_agent report).  Two signals from the same
   source are correlated and do not count as independent evidence.

This is distinct from ``agents/diagnostic_classifier.py``, which is the
full-featured LLM-backed classifier operating on ``FailureEvent`` +
``InfraTelemetry``.  This module operates at the signal level — it is the
layer the escalation system queries when deciding which ladder's budget to
spend.

Typical usage::

    from diagnostic_classifier import (
        FailureClass,
        DiagnosticSignal,
        classify_failure,
        build_signals_from_error,
        should_condemn_worker,
    )

    # Parse error message into signals, then classify.
    signals = build_signals_from_error(error_msg, worker_id="gpu-01")
    classification = classify_failure(error_msg, signals, worker_id="gpu-01")

    if classification == FailureClass.INFRA:
        # Route to infra ladder — content budget is untouched.
        ...
    elif classification == FailureClass.CONTENT:
        # Route to content ladder — infra budget is untouched.
        ...
    else:
        # UNCLEAR — run short_diagnostic to gather more evidence.
        ...
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FailureClass enum
# ---------------------------------------------------------------------------

class FailureClass(str, Enum):
    """Classification of a pipeline failure.

    Determines which escalation ladder's budget is consumed:

    - CONTENT: artifact-level failure (bad prompt, QA rejection, visual
      concept mismatch).  Consumes the content ladder's media-specific
      budget.
    - INFRA: substrate failure (OOM, CUDA error, driver reset, network
      timeout, preemption).  Consumes the infra ladder's separate budget.
    - UNCLEAR: ambiguous or insufficient signals.  The caller should run
      a short diagnostic to gather more evidence before reclassifying.
    """

    CONTENT = "content"
    INFRA = "infra"
    UNCLEAR = "unclear"


# ---------------------------------------------------------------------------
# DiagnosticSignal dataclass
# ---------------------------------------------------------------------------

# Valid source values — the origin of the signal.
VALID_SIGNAL_SOURCES = frozenset(
    {
        "job_result",       # Direct job outcome (e.g. clip generation failed)
        "infra_agent",      # InfraAgent health poll result
        "worker_health",    # Worker health endpoint response
        "cuda_error",       # CUDA / GPU driver error
        "timeout",          # Network or job timeout
    }
)

# Valid signal_type values — what the signal says about the failure.
VALID_SIGNAL_TYPES = frozenset(
    {
        "content_fail",     # Signal points to a content-level problem
        "infra_fail",      # Signal points to an infrastructure-level problem
        "healthy",         # Worker / service is healthy (exonerating signal)
        "unhealthy",       # Worker / service is unhealthy (condemning signal)
    }
)


@dataclass(frozen=True)
class DiagnosticSignal:
    """A single piece of evidence about a pipeline failure.

    Signals are the atomic unit the classifier uses to determine whether a
    failure is content-rooted or infra-rooted.  Each signal carries its
    source (who produced it), type (what it indicates), details (arbitrary
    context), and timestamp (when it was observed).

    Independence rule: two signals are **independent** only if they come from
    different ``source`` values.  Two ``job_result`` signals are correlated
    (same observation mechanism) and do not independently confirm a
    diagnosis.  A ``job_result`` + ``infra_agent`` pair IS independent
    (different observation mechanisms).

    Attributes
    ----------
    source:
        Origin of the signal — one of :data:`VALID_SIGNAL_SOURCES`.
    signal_type:
        What the signal indicates — one of :data:`VALID_SIGNAL_TYPES`.
    details:
        Arbitrary dict carrying context (error codes, messages, etc.).
    timestamp:
        Unix timestamp of when the signal was observed.
    """

    source: str
    signal_type: str
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.source not in VALID_SIGNAL_SOURCES:
            raise ValueError(
                f"DiagnosticSignal.source must be one of "
                f"{sorted(VALID_SIGNAL_SOURCES)}, got {self.source!r}"
            )
        if self.signal_type not in VALID_SIGNAL_TYPES:
            raise ValueError(
                f"DiagnosticSignal.signal_type must be one of "
                f"{sorted(VALID_SIGNAL_TYPES)}, got {self.signal_type!r}"
            )

    def is_infra(self) -> bool:
        """True if this signal points to an infra-level failure."""
        return self.signal_type in ("infra_fail", "unhealthy")

    def is_content(self) -> bool:
        """True if this signal points to a content-level failure."""
        return self.signal_type == "content_fail"

    def is_exonerating(self) -> bool:
        """True if this signal indicates the worker is healthy."""
        return self.signal_type == "healthy"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return {
            "source": self.source,
            "signal_type": self.signal_type,
            "details": dict(self.details),
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Known error patterns for signal extraction
# ---------------------------------------------------------------------------

# Infra patterns — each tuple is (signal_name, regex, detail_keys).
# Matches are case-insensitive.
_INFRA_ERROR_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "cuda_error",
        re.compile(
            r"\b(?:CUDA\s*(?:error|runtime|driver|assert|failure|kernel)|"
            r"cuDNN|NCCL)\b",
            re.I,
        ),
        "CUDA/GPU driver error detected",
    ),
    (
        "oom",
        re.compile(
            r"\b(?:out\s+of\s+memory|OOM|cuda\s+out\s+of\s+memory|"
            r"memory\s+error|VRAM\s+exhausted)\b",
            re.I,
        ),
        "Out-of-memory error detected",
    ),
    (
        "driver_reset",
        re.compile(
            r"\b(?:driver\s+reset|GPU\s+driver|nvidia-smi|NVML|"
            r"Xid-\d+)\b",
            re.I,
        ),
        "GPU driver reset or error detected",
    ),
    (
        "network_timeout",
        re.compile(
            r"\b(?:connection\s+(?:refused|reset|aborted|timed\s+out)|"
            r"timeout\s*(?:expired|exceeded|error)?|TimeoutError|"
            r"ReadTimeoutError|ECONNREFUSED|ECONNRESET|ETIMEDOUT|"
            r"EHOSTUNREACH|network\s+(?:partition|unreachable)|"
            r"no\s+route\s+to\s+host)\b",
            re.I,
        ),
        "Network timeout or partition detected",
    ),
    (
        "preemption",
        re.compile(
            r"\b(?:preempt(?:ed|ion)?|spot\s+interrupt|"
            r"instance\s+terminated|spot\s+termination)\b",
            re.I,
        ),
        "Instance preemption detected",
    ),
    (
        "cold_start_fail",
        re.compile(
            r"\b(?:cold\s+start|boot\s+failed|failed\s+to\s+boot|"
            r"bootstrap\s+failed|model\s+load\s+failed|"
            r"model\s+not\s+loaded)\b",
            re.I,
        ),
        "Cold-start or model load failure detected",
    ),
    (
        "process_crash",
        re.compile(
            r"\b(?:segfault|segmentation\s+fault|SIGKILL|SIGTERM|SIGSEGV|"
            r"process\s+(?:crashed|killed|died|exited\s+unexpectedly)|"
            r"core\s+dumped|worker\s+process\s+died|killed)\b",
            re.I,
        ),
        "Process crash detected",
    ),
]

# Content patterns — each tuple is (signal_name, regex, detail_keys).
_CONTENT_ERROR_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "bad_prompt",
        re.compile(
            r"\b(?:bad\s+prompt|prompt\s+(?:rejected|invalid|malformed|"
            r"too\s+short|empty|failed\s+policy)|unsafe\s+prompt)\b",
            re.I,
        ),
        "Bad or rejected prompt detected",
    ),
    (
        "visual_concept_mismatch",
        re.compile(
            r"\b(?:wrong\s+subject|off[- ]topic|shows\s+[a-z ]+\s+"
            r"instead\s+of|missing\s+(?:subject|setting)|"
            r"mismatch(?:ed)?\s+(?:content|subject)|visual\s+does\s+not\s+match|"
            r"visual\s+concept\s+mismatch)\b",
            re.I,
        ),
        "Visual concept mismatch detected",
    ),
    (
        "qa_structural_fail",
        re.compile(
            r"\b(?:QA[_\s]+(?:rejected|reject|structural)|"
            r"quality[:\s]+(?:rejected|structural\s+fail)|"
            r"gatekeeper\s+rejected|qa\s+verdict|"
            r"structural\s+(?:fail|violation|error))\b",
            re.I,
        ),
        "QA structural failure detected",
    ),
    (
        "qa_semantic_fail",
        re.compile(
            r"\b(?:QA[_\s]+(?:semantic|hints?)|quality[:\s]+semantic|"
            r"semantic\s+(?:fail|violation|mismatch)|"
            r"content\s+(?:fail|violation|mismatch))\b",
            re.I,
        ),
        "QA semantic failure detected",
    ),
    (
        "duration_mismatch",
        re.compile(
            r"\b(?:duration\s+(?:mismatch|violation|over|under)|"
            r"clip\s+(?:too\s+(?:long|short)|duration\s+fail)|"
            r"timing\s+(?:fail|violation|mismatch))\b",
            re.I,
        ),
        "Duration/timing mismatch detected",
    ),
]

# Fallback keyword sets for when no structured signals are available.
# Used by ``classify_failure`` to lean toward one axis based on the
# raw error message alone.
_INFRA_LEAN_KEYWORDS = frozenset(
    {
        "oom", "cuda", "timeout", "driver", "gpu", "vram",
        "preempt", "crash", "segfault", "network", "unreachable",
        "connection refused", "driver reset", "cold start",
    }
)

_CONTENT_LEAN_KEYWORDS = frozenset(
    {
        "prompt", "concept", "quality", "duration", "qa",
        "rejected", "mismatch", "bad prompt", "visual",
        "narration", "script", "off-topic",
    }
)


# ---------------------------------------------------------------------------
# build_signals_from_error
# ---------------------------------------------------------------------------

def build_signals_from_error(
    error_msg: str,
    worker_id: str = "",
) -> list[DiagnosticSignal]:
    """Parse an error message for known patterns and return diagnostic signals.

    Scans the error message against the known infra and content pattern
    registries.  Each match produces a ``DiagnosticSignal`` with
    ``source="job_result"`` (the signal originates from the job's error
    output) and the appropriate ``signal_type``.

    Parameters
    ----------
    error_msg:
        The raw error message or traceback from the failed job.
    worker_id:
        Optional worker identifier, stored in signal details for traceability.

    Returns
    -------
    list[DiagnosticSignal]
        One signal per matched pattern.  Empty if no patterns match.
    """
    if not error_msg:
        return []

    signals: list[DiagnosticSignal] = []
    now = time.time()

    # Scan infra patterns.
    for name, regex, detail_msg in _INFRA_ERROR_PATTERNS:
        if regex.search(error_msg):
            signals.append(
                DiagnosticSignal(
                    source="job_result",
                    signal_type="infra_fail",
                    details={
                        "pattern": name,
                        "message": detail_msg,
                        "worker_id": worker_id,
                        "error_snippet": error_msg[:200],
                    },
                    timestamp=now,
                )
            )

    # Scan content patterns.
    for name, regex, detail_msg in _CONTENT_ERROR_PATTERNS:
        if regex.search(error_msg):
            signals.append(
                DiagnosticSignal(
                    source="job_result",
                    signal_type="content_fail",
                    details={
                        "pattern": name,
                        "message": detail_msg,
                        "worker_id": worker_id,
                        "error_snippet": error_msg[:200],
                    },
                    timestamp=now,
                )
            )

    return signals


# ---------------------------------------------------------------------------
# classify_failure
# ---------------------------------------------------------------------------

def classify_failure(
    error_msg: str,
    signals: list[DiagnosticSignal],
    worker_id: str = "",
) -> FailureClass:
    """Classify a pipeline failure as content, infra, or unclear.

    Classification rules (per Diagram 8):

    1. **2+ infra signals** (CUDA error, OOM, driver reset, network timeout,
       preemption, cold-start fail) → :attr:`FailureClass.INFRA`.
    2. **2+ content signals** (bad prompt, visual concept mismatch, QA
       structural fail, QA semantic fail) → :attr:`FailureClass.CONTENT`.
    3. **Mixed signals** (both infra and content axes fire) →
       :attr:`FailureClass.UNCLEAR`.  The caller should run a short
       diagnostic to gather more evidence.
    4. **No signals**: fall back to keyword heuristics in the error message.
       OOM/CUDA/timeout/driver → lean INFRA; prompt/concept/quality/duration
       → lean CONTENT.  If no keywords match, → UNCLEAR.

    The "2+ signals" threshold is deliberate: a single signal is weak
    evidence.  The escalation system's budget isolation depends on correct
    classification — a single spurious infra signal must not drain the
    content ladder's budget, and vice versa.

    Parameters
    ----------
    error_msg:
        Raw error message or traceback from the failed job.
    signals:
        Pre-collected diagnostic signals (from :func:`build_signals_from_error`,
        infra_agent polls, worker health checks, etc.).
    worker_id:
        Optional worker identifier for logging and traceability.

    Returns
    -------
    FailureClass
        The classification — determines which ladder's budget is consumed.
    """
    infra_signals = [s for s in signals if s.is_infra()]
    content_signals = [s for s in signals if s.is_content()]

    n_infra = len(infra_signals)
    n_content = len(content_signals)

    # Rule 1: 2+ infra signals, no content signals → INFRA.
    if n_infra >= 2 and n_content == 0:
        logger.info(
            "classify_failure: INFRA (worker=%s) — %d infra signals, "
            "0 content signals: %s",
            worker_id,
            n_infra,
            ", ".join(s.details.get("pattern", s.signal_type) for s in infra_signals),
        )
        return FailureClass.INFRA

    # Rule 2: 2+ content signals, no infra signals → CONTENT.
    if n_content >= 2 and n_infra == 0:
        logger.info(
            "classify_failure: CONTENT (worker=%s) — %d content signals, "
            "0 infra signals: %s",
            worker_id,
            n_content,
            ", ".join(s.details.get("pattern", s.signal_type) for s in content_signals),
        )
        return FailureClass.CONTENT

    # Rule 3: Mixed signals → UNCLEAR.
    if n_infra > 0 and n_content > 0:
        logger.info(
            "classify_failure: UNCLEAR (worker=%s) — mixed signals: "
            "%d infra, %d content. Run short diagnostic.",
            worker_id,
            n_infra,
            n_content,
        )
        return FailureClass.UNCLEAR

    # If we have exactly 1 signal on one axis and 0 on the other, that's
    # not enough for a confident classification.  Fall through to keyword
    # heuristics, but the single signal can tip the balance.
    if n_infra == 1 and n_content == 0:
        logger.info(
            "classify_failure: single infra signal (worker=%s) — "
            "insufficient for confident classification, checking keywords",
            worker_id,
        )
    elif n_content == 1 and n_infra == 0:
        logger.info(
            "classify_failure: single content signal (worker=%s) — "
            "insufficient for confident classification, checking keywords",
            worker_id,
        )

    # Rule 4: No confident signals — fall back to keyword heuristics.
    return _classify_by_keywords(error_msg, worker_id, signals)


def _classify_by_keywords(
    error_msg: str,
    worker_id: str,
    signals: list[DiagnosticSignal],
) -> FailureClass:
    """Fall back to keyword heuristics when structured signals are insufficient.

    Scans the error message for known infra and content keywords.  If the
    single-signal axis agrees with the keyword heuristic, that's enough to
    classify.  If they disagree, we return UNCLEAR.

    Parameters
    ----------
    error_msg:
        Raw error message.
    worker_id:
        Worker identifier for logging.
    signals:
        The original signal list (used to check for single-axis agreement).

    Returns
    -------
    FailureClass
    """
    if not error_msg:
        logger.info(
            "classify_failure: UNCLEAR (worker=%s) — no error message "
            "and insufficient signals",
            worker_id,
        )
        return FailureClass.UNCLEAR

    lower_msg = error_msg.lower()

    infra_keyword_hits = [kw for kw in _INFRA_LEAN_KEYWORDS if kw in lower_msg]
    content_keyword_hits = [kw for kw in _CONTENT_LEAN_KEYWORDS if kw in lower_msg]

    n_infra_kw = len(infra_keyword_hits)
    n_content_kw = len(content_keyword_hits)

    # Check existing signals for single-axis agreement.
    has_infra_signal = any(s.is_infra() for s in signals)
    has_content_signal = any(s.is_content() for s in signals)

    # If keywords lean infra AND there's at least one infra signal,
    # that's corroborating evidence → INFRA.
    if n_infra_kw > 0 and n_content_kw == 0:
        if has_infra_signal and not has_content_signal:
            logger.info(
                "classify_failure: INFRA (worker=%s) — keyword heuristic "
                "corroborates single infra signal: %s",
                worker_id,
                ", ".join(infra_keyword_hits),
            )
            return FailureClass.INFRA
        if not has_content_signal:
            logger.info(
                "classify_failure: INFRA (worker=%s) — keyword heuristic "
                "leans infra (no content signals): %s",
                worker_id,
                ", ".join(infra_keyword_hits),
            )
            return FailureClass.INFRA

    # If keywords lean content AND there's at least one content signal,
    # that's corroborating evidence → CONTENT.
    if n_content_kw > 0 and n_infra_kw == 0:
        if has_content_signal and not has_infra_signal:
            logger.info(
                "classify_failure: CONTENT (worker=%s) — keyword heuristic "
                "corroborates single content signal: %s",
                worker_id,
                ", ".join(content_keyword_hits),
            )
            return FailureClass.CONTENT
        if not has_infra_signal:
            logger.info(
                "classify_failure: CONTENT (worker=%s) — keyword heuristic "
                "leans content (no infra signals): %s",
                worker_id,
                ", ".join(content_keyword_hits),
            )
            return FailureClass.CONTENT

    # Mixed keywords or keywords disagree with signals → UNCLEAR.
    if n_infra_kw > 0 and n_content_kw > 0:
        logger.info(
            "classify_failure: UNCLEAR (worker=%s) — mixed keywords: "
            "infra=%s, content=%s",
            worker_id,
            ", ".join(infra_keyword_hits),
            ", ".join(content_keyword_hits),
        )
    else:
        logger.info(
            "classify_failure: UNCLEAR (worker=%s) — no decisive keywords "
            "or keyword-signal disagreement",
            worker_id,
        )

    return FailureClass.UNCLEAR


# ---------------------------------------------------------------------------
# should_condemn_worker
# ---------------------------------------------------------------------------

def should_condemn_worker(
    worker_id: str,
    signals: list[DiagnosticSignal],
) -> bool:
    """Determine whether a worker should be condemned (removed from service).

    Per Diagram 8: "Workers are inferred from infra observation, never from
    job outcomes — a single failed clip does not condemn a worker, and a
    single good clip does not exonerate one."

    Condemnation requires **2+ INDEPENDENT infra signals**.  Two signals are
    independent if and only if they come from different ``source`` values.
    This prevents correlated-failure cascades where a single underlying
    issue produces multiple signals through the same observation mechanism.

    Examples of independent signal pairs:

    - ``job_result`` (clip failed with OOM) + ``infra_agent`` (infra agent
      reports worker unreachable) → CONDEMN.
    - ``job_result`` (CUDA error) + ``cuda_error`` (direct GPU error
      report) → CONDEMN.
    - ``job_result`` (OOM) + ``job_result`` (another OOM) → NOT independent;
      both come from the same source → DO NOT CONDEMN.
    - ``infra_agent`` (degraded) + ``worker_health`` (unhealthy) → CONDEMN.

    Parameters
    ----------
    worker_id:
        Identifier of the worker under evaluation.
    signals:
        Diagnostic signals collected for this worker.

    Returns
    -------
    bool
        True if the worker should be condemned (2+ independent infra
        signals).  False otherwise — the worker may still be suspect but
        does not have enough independent evidence for condemnation.
    """
    # Filter to infra-failing signals only.
    infra_signals = [s for s in signals if s.is_infra()]

    if len(infra_signals) < 2:
        logger.info(
            "should_condemn_worker: NO (worker=%s) — only %d infra signal(s), "
            "need 2+ independent signals",
            worker_id,
            len(infra_signals),
        )
        return False

    # Count distinct sources among infra signals.
    distinct_sources: set[str] = set()
    for s in infra_signals:
        distinct_sources.add(s.source)

    if len(distinct_sources) >= 2:
        logger.info(
            "should_condemn_worker: YES (worker=%s) — %d infra signals "
            "from %d independent sources: %s",
            worker_id,
            len(infra_signals),
            len(distinct_sources),
            sorted(distinct_sources),
        )
        return True

    # All infra signals come from the same source — correlated, not independent.
    logger.info(
        "should_condemn_worker: NO (worker=%s) — %d infra signals but all "
        "from same source %r (correlated, not independent)",
        worker_id,
        len(infra_signals),
        infra_signals[0].source,
    )
    return False


# ---------------------------------------------------------------------------
# short_diagnostic
# ---------------------------------------------------------------------------

def short_diagnostic(
    worker_url: str,
    capability: str = "tts",
) -> dict[str, Any]:
    """Run a quick health check on a worker to reclassify UNCLEAR failures.

    Probes the worker's ``/health`` endpoint to determine whether it is
    currently responsive.  This is used when the initial classification is
    UNCLEAR — a healthy worker suggests the failure was content-rooted
    (the worker is fine, the clip was bad), while an unhealthy or
    unreachable worker suggests the failure was infra-rooted.

    The diagnostic is intentionally lightweight — it does NOT run a full
    model inference or check GPU health in depth.  It answers one question:
    "is the worker alive and reporting healthy right now?"

    Parameters
    ----------
    worker_url:
        URL of the worker to probe (e.g. ``"http://1.2.3.4:8000"``).
    capability:
        Capability to check on the worker (``"tts"`` or ``"ltx"``).
        Defaults to ``"tts"``.

    Returns
    -------
    dict
        ``{"worker_url": str, "healthy": bool, "details": dict}``
        The ``details`` dict includes the raw health response (or error
        message if the probe failed) and the capability check result.
    """
    result: dict[str, Any] = {
        "worker_url": worker_url,
        "healthy": False,
        "details": {},
    }

    if not worker_url:
        result["details"] = {"error": "worker_url is empty"}
        logger.info("short_diagnostic: no worker_url provided")
        return result

    health_url = f"{worker_url.rstrip('/')}/"
    try:
        req = Request(health_url)
        with urlopen(req, timeout=10) as resp:
            text = resp.read().decode().strip()

        parts = text.split()
        status_ok = parts[0] == "ok" if parts else False
        model_loaded = f"{capability}=yes" in text

        result["healthy"] = status_ok and model_loaded
        result["details"] = {
            "status": parts[0] if parts else "",
            f"{capability}_loaded": model_loaded,
            "health_text": text,
        }

        logger.info(
            "short_diagnostic: worker %s healthy=%s (status=%s, %s=%s)",
            worker_url,
            result["healthy"],
            parts[0] if parts else "",
            f"{capability}_loaded",
            model_loaded,
        )

    except Exception as exc:
        result["healthy"] = False
        result["details"] = {
            "error": f"Health probe failed: {type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__,
        }
        logger.info(
            "short_diagnostic: worker %s unreachable (%s: %s)",
            worker_url,
            type(exc).__name__,
            exc,
        )

    return result


# ---------------------------------------------------------------------------
# Convenience: classify + diagnose in one call
# ---------------------------------------------------------------------------

def classify_with_diagnostic(
    error_msg: str,
    signals: list[DiagnosticSignal],
    worker_id: str = "",
    worker_url: str = "",
    capability: str = "tts",
) -> tuple[FailureClass, dict[str, Any]]:
    """Classify a failure, running a short diagnostic if initial result is UNCLEAR.

    This is a convenience function that combines :func:`classify_failure` and
    :func:`short_diagnostic`.  If the initial classification is UNCLEAR, it
    runs a diagnostic on the worker and uses the result to reclassify:

    - If the worker is healthy → lean CONTENT (the worker is fine, the clip
      was probably bad).
    - If the worker is unhealthy → lean INFRA (the worker itself is the
      problem).
    - If the diagnostic also fails → remain UNCLEAR.

    Parameters
    ----------
    error_msg:
        Raw error message or traceback.
    signals:
        Pre-collected diagnostic signals.
    worker_id:
        Worker identifier for logging.
    worker_url:
        Worker URL for the health probe (needed for reclassification).
    capability:
        Capability to check on the worker (``"tts"`` or ``"ltx"``).

    Returns
    -------
    tuple[FailureClass, dict[str, Any]]
        The final classification and a dict with the diagnostic result
        (empty if no diagnostic was needed).
    """
    initial = classify_failure(error_msg, signals, worker_id=worker_id)

    if initial != FailureClass.UNCLEAR:
        return initial, {}

    # Run short diagnostic to gather more evidence.
    diag = short_diagnostic(worker_url, capability=capability)

    if diag["healthy"]:
        # Worker is healthy → failure is likely content-rooted.
        logger.info(
            "classify_with_diagnostic: reclassified UNCLEAR→CONTENT "
            "(worker=%s, diagnostic=healthy)",
            worker_id,
        )
        return FailureClass.CONTENT, diag

    if not diag["healthy"] and "error" not in diag.get("details", {}):
        # Worker is reachable but unhealthy → failure is likely infra-rooted.
        logger.info(
            "classify_with_diagnostic: reclassified UNCLEAR→INFRA "
            "(worker=%s, diagnostic=unhealthy)",
            worker_id,
        )
        return FailureClass.INFRA, diag

    # Diagnostic failed (worker unreachable) — could be infra, but we
    # can't be sure.  Remain UNCLEAR and let the caller escalate.
    logger.info(
        "classify_with_diagnostic: remains UNCLEAR (worker=%s, "
        "diagnostic failed or inconclusive)",
        worker_id,
    )
    return FailureClass.UNCLEAR, diag


__all__ = [
    # Types
    "FailureClass",
    "DiagnosticSignal",
    # Constants
    "VALID_SIGNAL_SOURCES",
    "VALID_SIGNAL_TYPES",
    # Functions
    "classify_failure",
    "should_condemn_worker",
    "build_signals_from_error",
    "short_diagnostic",
    "classify_with_diagnostic",
]
