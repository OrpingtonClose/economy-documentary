"""
Graduated recovery middleware — cross-cutting concern for intelligent error handling.

Instead of binary crash-or-swallow, every pipeline operation gets a 4-level
recovery ladder:

    Level 1  RETRY          Same strategy, transient failures (backoff)
    Level 2  CREATIVE       Different approach, same goal (change params)
    Level 3  ENVIRONMENTAL  Diagnose root cause (VRAM, model, API keys, disk)
    Level 4  HUMAN          Pause pipeline, present diagnosis via AG-UI

Usage as decorator::

    @with_recovery("generate_video", policy=VIDEO_POLICY)
    def generate_video_clip(prompt, duration, ...):
        ...

Usage as context manager::

    with RecoveryContext("tts_generation", policy=TTS_POLICY) as ctx:
        result = generate_narration(...)

The middleware is a **plugin** — it wraps operations at any granularity (tool
call, stage callback, full pipeline) without modifying the operation itself.

Architecture invariant: recovery never silently degrades.  If all 4 levels
are exhausted, the pipeline stops with a clear error that includes the full
recovery history (what was tried at each level).
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
import threading
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Optional, TypeVar
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Recovery levels
# ---------------------------------------------------------------------------

class RecoveryLevel(IntEnum):
    """Graduated recovery levels — each escalates effort and intelligence."""
    RETRY = 1           # Same strategy, transient failures
    CREATIVE = 2        # Different approach, same goal
    ENVIRONMENTAL = 3   # Diagnose root cause
    HUMAN = 4           # Escalate to human via AG-UI


# ---------------------------------------------------------------------------
# Recovery policy (configurable per operation type)
# ---------------------------------------------------------------------------

@dataclass
class RecoveryPolicy:
    """Configurable recovery behaviour for an operation type.

    Each field controls one level of the recovery ladder.  Set a budget
    to 0 to skip that level entirely.
    """
    # Level 1: retry
    max_retries: int = 3
    retry_backoff_base: float = 2.0      # seconds; exponential backoff
    retry_backoff_max: float = 60.0      # cap on backoff delay
    retryable_exceptions: tuple[type[Exception], ...] = (
        ConnectionError, TimeoutError, URLError, OSError,
    )

    # Level 2: creative amendment
    creative_budget: int = 2
    creative_amendments: Optional[list[Callable[[dict], dict]]] = None
    # ^-- list of callables that take the current kwargs and return amended
    #     kwargs.  Applied in order.  If None, Level 2 is skipped.

    # Level 3: environmental assessment
    enable_env_assessment: bool = True

    # Level 4: human escalation
    escalate_to_human: bool = True
    human_timeout_sec: float = 600.0     # max wait for human response

    # Non-retryable: errors that should skip L1/L2/L3 and go straight to
    # human escalation (L4).  These indicate fundamental problems that
    # retrying or amending won't fix (e.g. QA REJECTED corrupted output).
    non_retryable_patterns: tuple[str, ...] = ()


# ── Pre-built policies for common operation types ─────────────────────────

def _video_amend_seed(kwargs: dict) -> dict:
    """Creative amendment: try a different random seed."""
    import random
    kwargs = dict(kwargs)
    kwargs["seed"] = random.randint(0, 2**32 - 1)
    logger.info("Recovery L2: amended video seed to %s", kwargs["seed"])
    return kwargs


_MIN_INFERENCE_STEPS = 25  # GAP 5.2: Never go below this floor


def _video_amend_steps(kwargs: dict) -> dict:
    """Creative amendment: reduce inference steps (faster, different result).

    GAP 5.2: Enforces a minimum floor of _MIN_INFERENCE_STEPS to prevent
    quality degradation below usable levels.
    """
    kwargs = dict(kwargs)
    steps = kwargs.get("num_inference_steps", 40)
    new_steps = max(steps - 10, _MIN_INFERENCE_STEPS)
    if new_steps == steps:
        # Would hit floor — return unchanged to signal no more step reductions
        logger.info("Recovery L2: inference steps already at floor (%d), skipping reduction", steps)
        return kwargs
    kwargs["num_inference_steps"] = new_steps
    logger.info("Recovery L2: reduced inference steps to %s (floor=%d)", new_steps, _MIN_INFERENCE_STEPS)
    return kwargs


def _tts_amend_chunk(kwargs: dict) -> dict:
    """Creative amendment: shorten text to reduce TTS failure probability."""
    kwargs = dict(kwargs)
    text = kwargs.get("text", "")
    if len(text) > 200:
        # Truncate at last sentence boundary before midpoint
        mid = len(text) // 2
        truncated = False
        for i in range(mid, 0, -1):
            if text[i] in ".!?":
                kwargs["text"] = text[:i + 1]
                truncated = True
                break
        if not truncated:
            # Fallback: hard-truncate at word boundary with ellipsis
            kwargs["text"] = text[:mid].rsplit(" ", 1)[0] + "..."
        logger.info("Recovery L2: chunked TTS text from %d to %d chars",
                     len(text), len(kwargs["text"]))
    return kwargs


VIDEO_POLICY = RecoveryPolicy(
    max_retries=3,
    retry_backoff_base=5.0,
    creative_budget=2,
    creative_amendments=[_video_amend_seed, _video_amend_steps],
    enable_env_assessment=True,
    escalate_to_human=True,
    # QA REJECTED = fundamentally broken output.  Don't waste time retrying
    # with different seeds or fewer steps — the model/config is wrong.
    non_retryable_patterns=("QA REJECTED",),
)

TTS_POLICY = RecoveryPolicy(
    max_retries=3,
    retry_backoff_base=3.0,
    creative_budget=1,
    creative_amendments=[_tts_amend_chunk],
    enable_env_assessment=True,
    escalate_to_human=True,
)

LLM_POLICY = RecoveryPolicy(
    max_retries=2,
    retry_backoff_base=2.0,
    creative_budget=0,  # LLM retries are usually sufficient
    enable_env_assessment=False,
    escalate_to_human=True,
)

B2_POLICY = RecoveryPolicy(
    max_retries=3,
    retry_backoff_base=2.0,
    creative_budget=0,
    enable_env_assessment=False,
    escalate_to_human=False,  # B2 failures are non-fatal (cache only)
)


# ---------------------------------------------------------------------------
# Recovery attempt tracking
# ---------------------------------------------------------------------------

@dataclass
class RecoveryAttempt:
    """Record of a single recovery attempt."""
    level: RecoveryLevel
    attempt_num: int
    error: str
    strategy: str           # what was tried ("retry #2", "amend seed", etc.)
    timestamp: float = 0.0
    success: bool = False
    amended_kwargs: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "level": self.level.name,
            "attempt": self.attempt_num,
            "error": self.error,
            "strategy": self.strategy,
            "timestamp": self.timestamp,
            "success": self.success,
        }


# ---------------------------------------------------------------------------
# Environmental assessment
# ---------------------------------------------------------------------------

@dataclass
class EnvironmentalDiagnosis:
    """Structured diagnosis from Level 3 environmental assessment."""
    root_cause: str                     # hypothesis
    confidence: str                     # "confirmed" | "likely" | "possible"
    checks: list[dict] = field(default_factory=list)  # individual check results
    proposed_fix: str = ""              # human-readable fix description
    proposed_action: str = ""           # machine-executable action identifier
    raw_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "root_cause": self.root_cause,
            "confidence": self.confidence,
            "checks": self.checks,
            "proposed_fix": self.proposed_fix,
            "proposed_action": self.proposed_action,
        }


class EnvironmentalAssessor:
    """Diagnoses systemic issues when creative amendments fail.

    Runs a battery of environment checks and synthesises a diagnosis.
    Each check is fast (< 5s) and non-destructive.
    """

    def diagnose(
        self,
        error: Exception,
        operation_name: str,
        context: Optional[dict] = None,
    ) -> EnvironmentalDiagnosis:
        """Run all checks and synthesise a diagnosis."""
        checks: list[dict] = []

        checks.append(self._check_vram())
        checks.append(self._check_disk())
        checks.append(self._check_workers())
        checks.append(self._check_api_keys())
        checks.append(self._check_network())

        # Synthesise
        return self._synthesize(error, operation_name, checks, context or {})

    def _check_vram(self) -> dict:
        """Check GPU VRAM usage via nvidia-smi."""
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total,name",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                gpus = []
                for line in lines:
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 3:
                        gpus.append({
                            "used_mb": int(parts[0]),
                            "total_mb": int(parts[1]),
                            "name": parts[2],
                            "utilization_pct": round(int(parts[0]) / max(int(parts[1]), 1) * 100, 1),
                        })
                return {
                    "check": "vram",
                    "status": "ok" if all(g["utilization_pct"] < 95 for g in gpus) else "warning",
                    "data": gpus,
                }
            return {"check": "vram", "status": "unavailable", "error": result.stderr[:200]}
        except Exception as e:
            return {"check": "vram", "status": "unavailable", "error": str(e)[:200]}

    def _check_disk(self) -> dict:
        """Check disk space on /tmp and working directories."""
        try:
            import shutil
            paths_to_check = ["/tmp", "/home/ubuntu"]
            results = {}
            for path in paths_to_check:
                if os.path.exists(path):
                    usage = shutil.disk_usage(path)
                    results[path] = {
                        "free_gb": round(usage.free / (1024**3), 1),
                        "total_gb": round(usage.total / (1024**3), 1),
                        "used_pct": round(usage.used / max(usage.total, 1) * 100, 1),
                    }
            any_low = any(v["free_gb"] < 5 for v in results.values())
            return {
                "check": "disk",
                "status": "warning" if any_low else "ok",
                "data": results,
            }
        except Exception as e:
            return {"check": "disk", "status": "error", "error": str(e)[:200]}

    def _check_workers(self) -> dict:
        """Check health of registered GPU/TTS workers."""
        try:
            from infra_agent import get_infra_agent
            agent = get_infra_agent()
            if agent is None:
                return {"check": "workers", "status": "unavailable", "error": "InfraAgent not running"}
            status = agent.get_status()
            workers = status.get("workers", [])
            healthy = sum(1 for w in workers if w.get("status") == "healthy")
            total = len(workers)
            return {
                "check": "workers",
                "status": "ok" if healthy == total else ("warning" if healthy > 0 else "critical"),
                "data": {"healthy": healthy, "total": total, "workers": workers},
            }
        except Exception as e:
            return {"check": "workers", "status": "error", "error": str(e)[:200]}

    def _check_api_keys(self) -> dict:
        """Check that required API keys are set (not their validity)."""
        keys_to_check = {
            "DASHSCOPE_API_KEY": "DashScope (Qwen visual QA)",
            "OPENAI_API_KEY": "OpenAI / OpenRouter (LLM)",
            "OPENAI_API_BASE": "LLM API base URL",
            "B2_APPLICATION_KEY_ID": "Backblaze B2",
            "B2_APPLICATION_KEY": "Backblaze B2",
        }
        results = {}
        missing = []
        for key, description in keys_to_check.items():
            val = os.environ.get(key, "")
            if val:
                results[key] = {"set": True, "description": description}
            else:
                results[key] = {"set": False, "description": description}
                missing.append(f"{key} ({description})")

        return {
            "check": "api_keys",
            "status": "warning" if missing else "ok",
            "data": results,
            "missing": missing,
        }

    def _check_network(self) -> dict:
        """Quick connectivity check to critical endpoints."""
        endpoints = {
            "openrouter": "https://openrouter.ai/api/v1/models",
            "dashscope": "https://dashscope.aliyuncs.com",
            "b2": "https://api.backblazeb2.com",
            "huggingface": "https://huggingface.co",
        }
        results = {}
        for name, url in endpoints.items():
            try:
                req = Request(url, method="HEAD")
                with urlopen(req, timeout=5):
                    results[name] = {"reachable": True}
            except Exception as e:
                results[name] = {"reachable": False, "error": str(e)[:100]}

        unreachable = [k for k, v in results.items() if not v.get("reachable")]
        return {
            "check": "network",
            "status": "warning" if unreachable else "ok",
            "data": results,
            "unreachable": unreachable,
        }

    def _synthesize(
        self,
        error: Exception,
        operation_name: str,
        checks: list[dict],
        context: dict,
    ) -> EnvironmentalDiagnosis:
        """Synthesise a diagnosis from individual check results."""
        error_str = str(error)

        # Check for VRAM exhaustion
        vram = next((c for c in checks if c["check"] == "vram"), None)
        if vram and vram.get("status") == "warning":
            gpus = vram.get("data", [])
            exhausted = [g for g in gpus if g.get("utilization_pct", 0) > 95]
            if exhausted:
                return EnvironmentalDiagnosis(
                    root_cause=f"GPU VRAM exhausted: {exhausted[0].get('used_mb')}MB / "
                               f"{exhausted[0].get('total_mb')}MB on {exhausted[0].get('name', 'GPU')}",
                    confidence="likely",
                    checks=checks,
                    proposed_fix="Free VRAM by unloading unused models or restarting the worker process",
                    proposed_action="restart_worker",
                )

        # Check for disk space
        disk = next((c for c in checks if c["check"] == "disk"), None)
        if disk and disk.get("status") == "warning":
            return EnvironmentalDiagnosis(
                root_cause="Disk space critically low",
                confidence="confirmed",
                checks=checks,
                proposed_fix="Free disk space by removing old pipeline outputs or temporary files",
                proposed_action="cleanup_disk",
            )

        # Check for worker health
        workers = next((c for c in checks if c["check"] == "workers"), None)
        if workers and workers.get("status") == "critical":
            return EnvironmentalDiagnosis(
                root_cause="All GPU/TTS workers are unreachable",
                confidence="confirmed",
                checks=checks,
                proposed_fix="Workers may have crashed or VMs may have been terminated. "
                             "Check Vast.ai dashboard and restart workers.",
                proposed_action="restart_workers",
            )

        # Check for missing API keys
        api_keys = next((c for c in checks if c["check"] == "api_keys"), None)
        if api_keys and api_keys.get("missing"):
            missing = api_keys["missing"]
            return EnvironmentalDiagnosis(
                root_cause=f"Missing API keys: {', '.join(missing)}",
                confidence="confirmed",
                checks=checks,
                proposed_fix=f"Set the following environment variables: {', '.join(missing)}",
                proposed_action="set_api_keys",
            )

        # Check for network issues
        network = next((c for c in checks if c["check"] == "network"), None)
        if network and network.get("unreachable"):
            unreachable = network["unreachable"]
            return EnvironmentalDiagnosis(
                root_cause=f"Network connectivity issues: {', '.join(unreachable)} unreachable",
                confidence="likely",
                checks=checks,
                proposed_fix=f"Check network connectivity to: {', '.join(unreachable)}. "
                             f"May need VPN or the services may be down.",
                proposed_action="check_network",
            )

        # Generic: couldn't identify specific root cause
        return EnvironmentalDiagnosis(
            root_cause=f"Unknown root cause for {operation_name} failure: {error_str[:200]}",
            confidence="possible",
            checks=checks,
            proposed_fix=f"Manual investigation needed. Error: {error_str[:300]}",
            proposed_action="manual_investigation",
        )


# Singleton assessor
_assessor = EnvironmentalAssessor()


# ---------------------------------------------------------------------------
# Human escalation
# ---------------------------------------------------------------------------

@dataclass
class HumanEscalationRequest:
    """A request for human input, sent via AG-UI."""
    id: str
    operation_name: str
    error_chain: list[dict]       # RecoveryAttempt.to_dict() for each attempt
    diagnosis: dict               # EnvironmentalDiagnosis.to_dict()
    proposed_actions: list[dict]  # [{action_id, description, risk_level}]
    severity: str                 # "warning" | "critical"
    timestamp: float = 0.0
    response: Optional[dict] = None       # filled when human responds
    resolved: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "operation_name": self.operation_name,
            "error_chain": self.error_chain,
            "diagnosis": self.diagnosis,
            "proposed_actions": self.proposed_actions,
            "severity": self.severity,
            "timestamp": self.timestamp,
            "resolved": self.resolved,
            "response": self.response,
        }


# Global escalation registry — AG-UI endpoints read/write this
_escalation_lock = threading.Lock()
_pending_escalations: dict[str, HumanEscalationRequest] = {}
_escalation_counter = 0


def _next_escalation_id() -> str:
    global _escalation_counter
    with _escalation_lock:
        _escalation_counter += 1
        return f"esc-{_escalation_counter:04d}-{int(time.time())}"


def submit_escalation(req: HumanEscalationRequest) -> None:
    """Submit an escalation request for human review."""
    with _escalation_lock:
        _pending_escalations[req.id] = req
    logger.critical(
        "RECOVERY L4 — HUMAN ESCALATION [%s]: %s | Diagnosis: %s",
        req.id, req.operation_name, req.diagnosis.get("root_cause", "unknown"),
    )
    # Emit SSE event for AG-UI dashboard
    _emit_escalation_event(req)


def resolve_escalation(escalation_id: str, response: dict) -> bool:
    """Resolve an escalation with a human response."""
    with _escalation_lock:
        req = _pending_escalations.get(escalation_id)
        if req is None:
            return False
        req.response = response
        req.resolved = True
    logger.info(
        "Escalation %s resolved by human: action=%s",
        escalation_id, response.get("action", "unknown"),
    )
    _emit_escalation_event(req)
    return True


def get_pending_escalations() -> list[dict]:
    """Get all pending (unresolved) escalation requests."""
    with _escalation_lock:
        return [
            req.to_dict()
            for req in _pending_escalations.values()
            if not req.resolved
        ]


def get_all_escalations() -> list[dict]:
    """Get all escalation requests (resolved and pending)."""
    with _escalation_lock:
        return [req.to_dict() for req in _pending_escalations.values()]


def _emit_escalation_event(req: HumanEscalationRequest) -> None:
    """Emit an SSE event for the AG-UI dashboard."""
    try:
        from agui import emit_agui_event
        emit_agui_event("escalation", req.to_dict())
    except ImportError:
        pass  # agui module not yet loaded


def _wait_for_human_response(
    escalation_id: str,
    timeout_sec: float = 600.0,
) -> Optional[dict]:
    """Block until the human responds to an escalation or timeout.

    Returns the human's response dict, or None on timeout.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        with _escalation_lock:
            req = _pending_escalations.get(escalation_id)
            if req and req.resolved:
                return req.response
        time.sleep(2.0)  # poll every 2 seconds

    logger.warning(
        "Escalation %s timed out after %.0fs — no human response",
        escalation_id, timeout_sec,
    )
    return None


# ---------------------------------------------------------------------------
# Recovery execution engine
# ---------------------------------------------------------------------------

class RecoveryExhausted(RuntimeError):
    """Raised when all recovery levels are exhausted.

    Carries the full recovery history so the caller (or crash handler)
    can report exactly what was tried.
    """
    def __init__(
        self,
        operation_name: str,
        original_error: Exception,
        attempts: list[RecoveryAttempt],
        diagnosis: Optional[EnvironmentalDiagnosis] = None,
    ):
        self.operation_name = operation_name
        self.original_error = original_error
        self.attempts = attempts
        self.diagnosis = diagnosis
        summary = (
            f"All recovery levels exhausted for '{operation_name}'. "
            f"Original error: {original_error}. "
            f"Attempts: {len(attempts)} across {len(set(a.level for a in attempts))} levels."
        )
        if diagnosis:
            summary += f" Diagnosis: {diagnosis.root_cause}"
        super().__init__(summary)

    def to_dict(self) -> dict:
        return {
            "operation": self.operation_name,
            "original_error": str(self.original_error),
            "attempts": [a.to_dict() for a in self.attempts],
            "diagnosis": self.diagnosis.to_dict() if self.diagnosis else None,
        }


def execute_with_recovery(
    operation: Callable,
    operation_name: str,
    kwargs: dict,
    policy: RecoveryPolicy,
    context: Optional[dict] = None,
) -> Any:
    """Execute an operation with graduated recovery.

    This is the core engine that drives the recovery ladder.

    Args:
        operation: The callable to execute.
        operation_name: Human-readable name for logging/escalation.
        kwargs: Keyword arguments to pass to the operation.
        policy: Recovery policy controlling behaviour at each level.
        context: Optional context dict (pipeline state, metadata).

    Returns:
        The operation's return value on success.

    Raises:
        RecoveryExhausted: If all recovery levels are exhausted.
    """
    attempts: list[RecoveryAttempt] = []
    current_kwargs = dict(kwargs)
    last_error: Optional[Exception] = None
    diagnosis: Optional[EnvironmentalDiagnosis] = None

    # ── Check for non-retryable patterns ──────────────────────────────
    # Some errors indicate fundamental problems (e.g. QA REJECTED corrupted
    # output) where retrying, amending, or diagnosing won't help.  Skip
    # straight to human escalation.
    def _is_non_retryable(err: Exception) -> bool:
        err_str = str(err)
        return any(pat in err_str for pat in policy.non_retryable_patterns)

    # ── Level 1: Retry ────────────────────────────────────────────────
    for retry_num in range(1, policy.max_retries + 1):
        try:
            result = operation(**current_kwargs)
            if attempts:
                logger.info(
                    "Recovery L1: '%s' succeeded on retry %d/%d",
                    operation_name, retry_num, policy.max_retries,
                )
            return result
        except Exception as e:
            last_error = e

            # Non-retryable error — skip all recovery levels, go to escalation
            if _is_non_retryable(e):
                logger.error(
                    "Recovery: '%s' hit non-retryable error, skipping to escalation: %s",
                    operation_name, str(e)[:300],
                )
                attempts.append(RecoveryAttempt(
                    level=RecoveryLevel.RETRY,
                    attempt_num=retry_num,
                    error=str(e)[:500],
                    strategy="non-retryable error — skipping to human escalation",
                    timestamp=time.time(),
                ))
                break

            is_retryable = isinstance(e, policy.retryable_exceptions)
            attempt = RecoveryAttempt(
                level=RecoveryLevel.RETRY,
                attempt_num=retry_num,
                error=str(e)[:500],
                strategy=f"retry {retry_num}/{policy.max_retries} (backoff)",
                timestamp=time.time(),
            )
            attempts.append(attempt)

            if not is_retryable or retry_num == policy.max_retries:
                logger.warning(
                    "Recovery L1: '%s' failed after %d retries: %s",
                    operation_name, retry_num, str(e)[:200],
                )
                break

            backoff = min(
                policy.retry_backoff_base * (2 ** (retry_num - 1)),
                policy.retry_backoff_max,
            )
            logger.info(
                "Recovery L1: '%s' retry %d/%d in %.1fs: %s",
                operation_name, retry_num, policy.max_retries, backoff, str(e)[:100],
            )
            time.sleep(backoff)

    # ── Skip L2/L3 for non-retryable errors ───────────────────────────
    _skip_to_escalation = last_error is not None and _is_non_retryable(last_error)

    # ── Level 2: Creative amendment ───────────────────────────────────
    amendments = policy.creative_amendments or []
    if _skip_to_escalation:
        logger.info("Recovery: skipping L2 (creative) for non-retryable error")
        amendments = []  # skip all amendments
    for amend_num, amend_fn in enumerate(amendments[:policy.creative_budget], 1):
        try:
            current_kwargs = amend_fn(current_kwargs)
            result = operation(**current_kwargs)
            logger.info(
                "Recovery L2: '%s' succeeded with creative amendment %d",
                operation_name, amend_num,
            )
            attempts.append(RecoveryAttempt(
                level=RecoveryLevel.CREATIVE,
                attempt_num=amend_num,
                error="",
                strategy=f"creative amendment: {amend_fn.__name__}",
                timestamp=time.time(),
                success=True,
                amended_kwargs={k: str(v)[:100] for k, v in current_kwargs.items()},
            ))
            return result
        except Exception as e:
            last_error = e
            attempts.append(RecoveryAttempt(
                level=RecoveryLevel.CREATIVE,
                attempt_num=amend_num,
                error=str(e)[:500],
                strategy=f"creative amendment: {amend_fn.__name__}",
                timestamp=time.time(),
            ))
            logger.warning(
                "Recovery L2: '%s' creative amendment %d (%s) failed: %s",
                operation_name, amend_num, amend_fn.__name__, str(e)[:200],
            )

    # ── Level 3: Environmental assessment ─────────────────────────────
    if _skip_to_escalation:
        logger.info("Recovery: skipping L3 (environmental) for non-retryable error")
    elif policy.enable_env_assessment and last_error is not None:
        logger.info("Recovery L3: '%s' — running environmental assessment", operation_name)
        diagnosis = _assessor.diagnose(last_error, operation_name, context)
        attempts.append(RecoveryAttempt(
            level=RecoveryLevel.ENVIRONMENTAL,
            attempt_num=1,
            error=str(last_error)[:500],
            strategy=f"environmental assessment: {diagnosis.root_cause}",
            timestamp=time.time(),
        ))
        logger.warning(
            "Recovery L3: '%s' diagnosis: %s (confidence: %s). Proposed: %s",
            operation_name, diagnosis.root_cause,
            diagnosis.confidence, diagnosis.proposed_fix,
        )

    # ── Level 4: Human escalation ─────────────────────────────────────
    if policy.escalate_to_human and last_error is not None:
        escalation_id = _next_escalation_id()
        diag_dict = diagnosis.to_dict() if diagnosis else {
            "root_cause": f"Unknown failure in {operation_name}",
            "confidence": "possible",
            "proposed_fix": "Manual investigation needed",
        }
        proposed_actions = [
            {
                "action_id": "retry_with_fix",
                "description": diagnosis.proposed_fix if diagnosis else "Retry after manual fix",
                "risk_level": "low",
            },
            {
                "action_id": "skip",
                "description": f"Skip {operation_name} and continue pipeline",
                "risk_level": "medium",
            },
            {
                "action_id": "abort",
                "description": "Abort the pipeline",
                "risk_level": "high",
            },
        ]

        req = HumanEscalationRequest(
            id=escalation_id,
            operation_name=operation_name,
            error_chain=[a.to_dict() for a in attempts],
            diagnosis=diag_dict,
            proposed_actions=proposed_actions,
            severity="critical" if len(attempts) > 3 else "warning",
            timestamp=time.time(),
        )
        submit_escalation(req)

        logger.critical(
            "Recovery L4: '%s' — waiting for human response (escalation %s, timeout %.0fs)",
            operation_name, escalation_id, policy.human_timeout_sec,
        )
        human_response = _wait_for_human_response(escalation_id, policy.human_timeout_sec)

        if human_response:
            action = human_response.get("action", "")
            if action == "retry_with_fix":
                # Human approved a fix — try one more time
                logger.info("Recovery L4: human approved retry for '%s'", operation_name)
                try:
                    result = operation(**current_kwargs)
                    attempts.append(RecoveryAttempt(
                        level=RecoveryLevel.HUMAN,
                        attempt_num=1,
                        error="",
                        strategy="human-approved retry",
                        timestamp=time.time(),
                        success=True,
                    ))
                    return result
                except Exception as e:
                    last_error = e
                    attempts.append(RecoveryAttempt(
                        level=RecoveryLevel.HUMAN,
                        attempt_num=1,
                        error=str(e)[:500],
                        strategy="human-approved retry (failed)",
                        timestamp=time.time(),
                    ))
            elif action == "skip":
                logger.warning("Recovery L4: human chose to skip '%s'", operation_name)
                return None  # caller must handle None
            elif action == "abort":
                raise RecoveryExhausted(
                    operation_name=operation_name,
                    original_error=last_error,
                    attempts=attempts,
                    diagnosis=diagnosis,
                )
            elif action == "amend":
                # Human provided amended kwargs
                amended = human_response.get("kwargs", {})
                if amended:
                    current_kwargs.update(amended)
                    try:
                        result = operation(**current_kwargs)
                        return result
                    except Exception as e:
                        last_error = e

    # All levels exhausted
    raise RecoveryExhausted(
        operation_name=operation_name,
        original_error=last_error or RuntimeError(f"{operation_name} failed with no error captured"),
        attempts=attempts,
        diagnosis=diagnosis,
    )


# ---------------------------------------------------------------------------
# Decorator API
# ---------------------------------------------------------------------------

def with_recovery(
    operation_name: str,
    policy: Optional[RecoveryPolicy] = None,
) -> Callable[[F], F]:
    """Decorator that wraps a function with graduated recovery.

    Usage::

        @with_recovery("generate_video", policy=VIDEO_POLICY)
        def generate_video_clip(prompt, duration, seed=42):
            ...

    The decorated function gains automatic retry, creative amendment,
    environmental assessment, and human escalation.

    Positional arguments are converted to keyword arguments using the
    function's signature so that creative amendments can modify them.
    """
    import inspect

    _policy = policy or RecoveryPolicy()

    def decorator(fn: F) -> F:
        _sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Bind positional args to their parameter names so creative
            # amendments (which operate on kwargs dicts) can modify them.
            bound = _sig.bind(*args, **kwargs)
            bound.apply_defaults()
            all_kwargs = dict(bound.arguments)
            return execute_with_recovery(
                operation=fn,
                operation_name=operation_name,
                kwargs=all_kwargs,
                policy=_policy,
            )
        return wrapper  # type: ignore[return-value]
    return decorator
