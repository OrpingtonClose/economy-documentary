"""
Graduated recovery middleware — cross-cutting concern for intelligent error handling.

Instead of binary crash-or-swallow, every pipeline operation gets a 4-level
recovery ladder:

    Level 0  FIX            Domain specialist agent rewrites inputs to fix
    Level 1  RETRY          Intelligent retry agent analyses error patterns
    Level 2  CREATIVE       Alternative strategy agent brainstorms new approach
    Level 3  COLLABORATIVE  Inter-agent coordination (talks to other agents)
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
    """Graduated recovery levels — each has an LLM-powered agent.

    Every level's agent gets LLM access, tool access, and full diagnostic
    context.  The agent at each level has increasing authority and scope:

        FIX (0)           Domain specialist — rewrites inputs to fix the specific problem
        RETRY (1)         Intelligent retry — checks health, analyses error patterns
        CREATIVE (2)      Alternative strategies — different model, different approach
        COLLABORATIVE (3) Inter-agent — talks to other pipeline agents to coordinate
        HUMAN (4)         Last resort — presents full chain to human via AG-UI
    """
    FIX = 0              # Domain-specific intelligent fix (LLM agent)
    RETRY = 1            # Intelligent retry (LLM agent)
    CREATIVE = 2         # Alternative strategy (LLM agent)
    COLLABORATIVE = 3    # Inter-agent coordination (LLM agent)
    HUMAN = 4            # Escalate to human via AG-UI


# ---------------------------------------------------------------------------
# Recovery policy (configurable per operation type)
# ---------------------------------------------------------------------------

@dataclass
class RecoveryPolicy:
    """Configurable recovery behaviour for an operation type.

    Each level of the ladder has an LLM-powered agent.  The ``agents``
    dict maps ``RecoveryLevel`` (int) to a ``RecoveryAgent`` instance.
    The ``level_budgets`` dict controls how many attempts each level gets.

    Legacy fields (max_retries, creative_amendments, etc.) are still
    supported for backward compatibility — they're used when no agent
    is configured for that level.
    """
    # ── Agent-powered recovery (new) ──────────────────────────────────
    agents: Optional[dict] = None
    # ^-- dict[int, RecoveryAgent] mapping level number to agent instance.
    #     Import RecoveryAgent from recovery_agents to avoid circular imports.
    #     Example: {0: AudioTimingAgent(), 1: RetryAgent(), 2: CreativeAgent(), 3: CollaborativeAgent()}

    level_budgets: Optional[dict] = None
    # ^-- dict[int, int] mapping level number to max attempts.
    #     Default: {0: 5, 1: 3, 2: 2, 3: 1}

    # ── Legacy fields (backward compat) ───────────────────────────────
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

    # Level 3: environmental assessment (legacy — replaced by CollaborativeAgent)
    enable_env_assessment: bool = True

    # Level 4: human escalation
    escalate_to_human: bool = True
    human_timeout_sec: float = 600.0     # max wait for human response

    # Non-retryable: errors that should skip L0/L1/L2/L3 and go straight to
    # human escalation (L4).
    non_retryable_patterns: tuple[str, ...] = ()

    def get_level_budget(self, level: int) -> int:
        """Get the attempt budget for a recovery level."""
        defaults = {0: 5, 1: 3, 2: 2, 3: 1}
        if self.level_budgets:
            return self.level_budgets.get(level, defaults.get(level, 1))
        return defaults.get(level, 1)

    def get_agent(self, level: int):
        """Get the agent for a recovery level, or None."""
        if self.agents:
            return self.agents.get(level)
        return None


# ── Pre-built policies for common operation types ─────────────────────────

def _video_amend_prompt_with_qa_hints(kwargs: dict) -> dict:
    """Creative amendment: inject corrective hints from QA feedback into the prompt.

    When a clip is QA-rejected, the QA reason often contains actionable
    feedback (e.g. "shows mountains instead of food", "missing kitchen
    setting").  This amendment parses the last error's QA reason and
    prepends corrective guidance to the prompt so the model is more
    likely to generate on-topic content on the next attempt.
    """
    import random
    kwargs = dict(kwargs)
    # Also randomise seed so we don't regenerate the exact same output
    kwargs["seed"] = random.randint(0, 2**32 - 1)

    # The QA reason is embedded in the RuntimeError message after "QA_HINTS:"
    # by _call_gpu_worker when quality is "rejected" or "poor".
    qa_hints = kwargs.pop("_qa_hints", "")
    if not qa_hints:
        # Fallback: no structured hints, just add a generic corrective prefix
        prompt = kwargs.get("prompt", "")
        kwargs["prompt"] = (
            "IMPORTANT: Generate content that precisely matches the described "
            "subject matter and visual style. Avoid generic landscapes or "
            "unrelated imagery. " + prompt
        )
        logger.info("Recovery L2: added generic corrective prefix to prompt (no QA hints)")
        return kwargs

    prompt = kwargs.get("prompt", "")
    kwargs["prompt"] = (
        f"CORRECTIVE GUIDANCE (previous attempt was rejected): {qa_hints}. "
        f"You MUST address the above feedback. "
        + prompt
    )
    logger.info(
        "Recovery L2: injected QA corrective hints into prompt: %s",
        qa_hints[:200],
    )
    return kwargs


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
    creative_budget=3,
    creative_amendments=[
        _video_amend_prompt_with_qa_hints,
        _video_amend_seed,
        _video_amend_steps,
    ],
    enable_env_assessment=True,
    escalate_to_human=True,
    non_retryable_patterns=(),
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
    creative_budget=0,
    enable_env_assessment=False,
    escalate_to_human=True,
)

B2_POLICY = RecoveryPolicy(
    max_retries=3,
    retry_backoff_base=2.0,
    creative_budget=0,
    enable_env_assessment=False,
    escalate_to_human=False,
)


# ── Agent-powered policies ────────────────────────────────────────────────
# These use LLM-powered agents at every level of the ladder.
# Import agents lazily to avoid circular imports at module load time.

def _make_audio_agent_policy() -> RecoveryPolicy:
    """Audio operations: L0 rewrites narration text to fix timing."""
    from recovery_agents import AUDIO_AGENTS
    return RecoveryPolicy(
        agents=AUDIO_AGENTS,
        level_budgets={0: 5, 1: 3, 2: 2, 3: 1},
        retry_backoff_base=3.0,
        escalate_to_human=True,
    )


def _make_video_agent_policy() -> RecoveryPolicy:
    """Video operations: L0 rewrites visual prompts based on QA feedback."""
    from recovery_agents import VIDEO_AGENTS
    return RecoveryPolicy(
        agents=VIDEO_AGENTS,
        level_budgets={0: 3, 1: 3, 2: 2, 3: 1},
        retry_backoff_base=5.0,
        escalate_to_human=True,
    )


def _make_production_agent_policy() -> RecoveryPolicy:
    """Production batch operations: L0 restructures batches."""
    from recovery_agents import PRODUCTION_AGENTS
    return RecoveryPolicy(
        agents=PRODUCTION_AGENTS,
        level_budgets={0: 3, 1: 2, 2: 2, 3: 1},
        retry_backoff_base=5.0,
        escalate_to_human=True,
    )


def _make_otio_agent_policy() -> RecoveryPolicy:
    """OTIO validation: L0 fixes timeline gaps and violations."""
    from recovery_agents import OTIO_AGENTS
    return RecoveryPolicy(
        agents=OTIO_AGENTS,
        level_budgets={0: 3, 1: 2, 2: 1, 3: 1},
        retry_backoff_base=2.0,
        escalate_to_human=True,
    )


def _make_generic_agent_policy() -> RecoveryPolicy:
    """Generic operations: no domain L0, starts at L1 retry agent."""
    from recovery_agents import GENERIC_AGENTS
    return RecoveryPolicy(
        agents=GENERIC_AGENTS,
        level_budgets={1: 3, 2: 2, 3: 1},
        retry_backoff_base=3.0,
        escalate_to_human=True,
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
    pipeline_state: Optional[dict] = None,
    diagnostic_data: Optional[dict] = None,
) -> Any:
    """Execute an operation with the agent-powered recovery ladder.

    Every level (0–3) has an LLM-powered agent that can diagnose the
    failure and alter the inputs to fix it.  Level 4 is human escalation.

    If the policy has ``agents`` configured, the agent-powered path is
    used.  Otherwise, falls back to the legacy retry/amend/assess path
    for backward compatibility.

    Args:
        operation: The callable to execute.
        operation_name: Human-readable name for logging/escalation.
        kwargs: Keyword arguments to pass to the operation.
        policy: Recovery policy controlling behaviour at each level.
        context: Optional context dict (metadata).
        pipeline_state: Optional pipeline state dict (for agent context).
        diagnostic_data: Optional domain-specific diagnostic data
            (e.g. timing analysis, QA results).

    Returns:
        The operation's return value on success.

    Raises:
        RecoveryExhausted: If all recovery levels are exhausted.
    """
    # If agents are configured, use the agent-powered path
    if policy.agents:
        return _execute_with_agents(
            operation, operation_name, kwargs, policy,
            context, pipeline_state, diagnostic_data,
        )
    # Otherwise, fall back to legacy path
    return _execute_legacy(
        operation, operation_name, kwargs, policy, context,
    )


def _execute_with_agents(
    operation: Callable,
    operation_name: str,
    kwargs: dict,
    policy: RecoveryPolicy,
    context: Optional[dict] = None,
    pipeline_state: Optional[dict] = None,
    diagnostic_data: Optional[dict] = None,
) -> Any:
    """Agent-powered recovery ladder.

    Levels 0–3 each have an LLM agent.  The agent receives full
    diagnostic context and returns a ``RecoveryDecision``:

    - ``fix``:      Apply state_patches, re-run the operation.
    - ``retry``:    Re-run without changes.
    - ``skip``:     Accept the failure, continue pipeline.
    - ``escalate``: Move to the next level.
    - ``abort``:    Stop the pipeline.
    """
    from recovery_agents import RecoveryContext  # noqa: F811

    attempts: list[RecoveryAttempt] = []
    current_kwargs = dict(kwargs)
    last_error: Optional[Exception] = None
    diagnosis: Optional[EnvironmentalDiagnosis] = None

    # Non-retryable check
    def _is_non_retryable(err: Exception) -> bool:
        err_str = str(err)
        return any(pat in err_str for pat in policy.non_retryable_patterns)

    # ── Initial execution ─────────────────────────────────────────────
    try:
        return operation(**current_kwargs)
    except Exception as e:
        last_error = e
        if _is_non_retryable(e):
            logger.error(
                "Recovery: '%s' non-retryable error, skipping to L4: %s",
                operation_name, str(e)[:300],
            )
            attempts.append(RecoveryAttempt(
                level=RecoveryLevel.FIX,
                attempt_num=0,
                error=str(e)[:500],
                strategy="initial execution — non-retryable",
                timestamp=time.time(),
            ))
            # Jump straight to human escalation
            return _escalate_to_human(
                operation, operation_name, current_kwargs, policy,
                last_error, attempts, diagnosis,
            )
        logger.info(
            "Recovery: '%s' failed, entering agent-powered ladder: %s",
            operation_name, str(e)[:200],
        )

    # ── Levels 0–3: Agent-powered recovery ────────────────────────────
    _level_names = {
        0: "FIX (domain specialist)",
        1: "RETRY (intelligent retry)",
        2: "CREATIVE (alternative strategy)",
        3: "COLLABORATIVE (inter-agent coordination)",
    }

    for level in range(4):  # 0, 1, 2, 3
        agent = policy.get_agent(level)
        if agent is None:
            continue  # No agent for this level — skip to next

        budget = policy.get_level_budget(level)
        level_name = _level_names.get(level, f"Level {level}")

        logger.info(
            "Recovery L%d (%s): '%s' — agent '%s' (budget: %d)",
            level, level_name, operation_name, agent.name, budget,
        )

        for attempt_num in range(1, budget + 1):
            # Build context for the agent
            agent_context = RecoveryContext(
                operation_name=operation_name,
                error_msg=str(last_error) if last_error else "Unknown error",
                current_level=level,
                level_name=level_name,
                attempt_num=attempt_num,
                max_attempts=budget,
                previous_attempts=[a.to_dict() for a in attempts],
                operation_kwargs=current_kwargs,
                pipeline_state=pipeline_state or {},
                diagnostic_data=diagnostic_data or {},
            )

            # Ask the agent what to do
            try:
                decision = agent.decide(agent_context)
            except Exception as agent_err:
                logger.error(
                    "Recovery L%d: agent '%s' crashed: %s",
                    level, agent.name, str(agent_err)[:300],
                )
                attempts.append(RecoveryAttempt(
                    level=RecoveryLevel(level),
                    attempt_num=attempt_num,
                    error=f"Agent crashed: {agent_err}",
                    strategy=f"agent '{agent.name}' error",
                    timestamp=time.time(),
                ))
                break  # Move to next level

            logger.info(
                "Recovery L%d: agent '%s' decided: action=%s confidence=%.2f — %s",
                level, agent.name, decision.action,
                decision.confidence, decision.explanation[:200],
            )

            attempts.append(RecoveryAttempt(
                level=RecoveryLevel(level),
                attempt_num=attempt_num,
                error=str(last_error)[:500] if last_error else "",
                strategy=f"agent '{agent.name}': {decision.action} — {decision.explanation[:200]}",
                timestamp=time.time(),
                success=decision.action in ("fix", "retry", "skip"),
                amended_kwargs=(
                    {k: str(v)[:100] for k, v in decision.state_patches.items()}
                    if decision.state_patches else None
                ),
            ))

            # ── Handle the decision ───────────────────────────────────
            if decision.action == "fix":
                # Apply state patches and re-run
                if decision.state_patches:
                    current_kwargs.update(decision.state_patches)
                    # Also update pipeline_state if patches are for it
                    if pipeline_state is not None:
                        pipeline_state.update(decision.state_patches)
                try:
                    result = operation(**current_kwargs)
                    logger.info(
                        "Recovery L%d: '%s' succeeded after agent fix (attempt %d/%d)",
                        level, operation_name, attempt_num, budget,
                    )
                    return result
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "Recovery L%d: '%s' still failed after agent fix: %s",
                        level, operation_name, str(e)[:200],
                    )
                    # Update diagnostic_data with new error for next attempt
                    if diagnostic_data is not None:
                        diagnostic_data["last_fix_error"] = str(e)[:500]
                    continue  # Try again at this level

            elif decision.action == "retry":
                try:
                    backoff = policy.retry_backoff_base * (2 ** (attempt_num - 1))
                    backoff = min(backoff, policy.retry_backoff_max)
                    time.sleep(backoff)
                    result = operation(**current_kwargs)
                    logger.info(
                        "Recovery L%d: '%s' succeeded on retry (attempt %d/%d)",
                        level, operation_name, attempt_num, budget,
                    )
                    return result
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "Recovery L%d: '%s' retry failed: %s",
                        level, operation_name, str(e)[:200],
                    )
                    continue

            elif decision.action == "skip":
                logger.warning(
                    "Recovery L%d: agent '%s' decided to skip '%s'",
                    level, agent.name, operation_name,
                )
                return None  # Caller must handle None

            elif decision.action == "abort":
                raise RecoveryExhausted(
                    operation_name=operation_name,
                    original_error=last_error or RuntimeError(f"{operation_name} aborted by agent"),
                    attempts=attempts,
                    diagnosis=diagnosis,
                )

            elif decision.action == "escalate":
                logger.info(
                    "Recovery L%d: agent '%s' escalated '%s' to next level",
                    level, agent.name, operation_name,
                )
                break  # Move to next level

        # End of budget for this level — move to next

    # ── Level 4: Human escalation ─────────────────────────────────────
    return _escalate_to_human(
        operation, operation_name, current_kwargs, policy,
        last_error, attempts, diagnosis,
    )


def _escalate_to_human(
    operation: Callable,
    operation_name: str,
    current_kwargs: dict,
    policy: RecoveryPolicy,
    last_error: Optional[Exception],
    attempts: list[RecoveryAttempt],
    diagnosis: Optional[EnvironmentalDiagnosis],
) -> Any:
    """Level 4: Human escalation — presents full diagnostic chain."""
    if not policy.escalate_to_human or last_error is None:
        raise RecoveryExhausted(
            operation_name=operation_name,
            original_error=last_error or RuntimeError(f"{operation_name} failed"),
            attempts=attempts,
            diagnosis=diagnosis,
        )

    escalation_id = _next_escalation_id()
    diag_dict = diagnosis.to_dict() if diagnosis else {
        "root_cause": f"All agent levels exhausted for {operation_name}",
        "confidence": "confirmed",
        "proposed_fix": "Manual investigation needed — agents at L0-L3 could not resolve.",
    }
    proposed_actions = [
        {
            "action_id": "retry_with_fix",
            "description": "Retry after manual fix",
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
        "Recovery L4: '%s' — waiting for human (escalation %s, timeout %.0fs). "
        "Agent chain: %d attempts across %d levels.",
        operation_name, escalation_id, policy.human_timeout_sec,
        len(attempts), len(set(a.level for a in attempts)),
    )
    human_response = _wait_for_human_response(escalation_id, policy.human_timeout_sec)

    if human_response:
        action = human_response.get("action", "")
        if action == "retry_with_fix":
            logger.info("Recovery L4: human approved retry for '%s'", operation_name)
            try:
                result = operation(**current_kwargs)
                return result
            except Exception as e:
                pass  # Fall through to exhausted
        elif action == "skip":
            logger.warning("Recovery L4: human chose to skip '%s'", operation_name)
            return None
        elif action == "abort":
            raise RecoveryExhausted(
                operation_name=operation_name,
                original_error=last_error,
                attempts=attempts,
                diagnosis=diagnosis,
            )
        elif action == "amend":
            amended = human_response.get("kwargs", {})
            if amended:
                current_kwargs.update(amended)
                try:
                    return operation(**current_kwargs)
                except Exception:
                    pass  # Fall through to exhausted

    # All levels exhausted
    raise RecoveryExhausted(
        operation_name=operation_name,
        original_error=last_error or RuntimeError(f"{operation_name} failed"),
        attempts=attempts,
        diagnosis=diagnosis,
    )


def _execute_legacy(
    operation: Callable,
    operation_name: str,
    kwargs: dict,
    policy: RecoveryPolicy,
    context: Optional[dict] = None,
) -> Any:
    """Legacy recovery path — retry/amend/assess without agents.

    Used when no agents are configured in the policy.
    """
    attempts: list[RecoveryAttempt] = []
    current_kwargs = dict(kwargs)
    last_error: Optional[Exception] = None
    diagnosis: Optional[EnvironmentalDiagnosis] = None

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

            if _is_non_retryable(e):
                logger.error(
                    "Recovery: '%s' hit non-retryable error: %s",
                    operation_name, str(e)[:300],
                )
                attempts.append(RecoveryAttempt(
                    level=RecoveryLevel.RETRY,
                    attempt_num=retry_num,
                    error=str(e)[:500],
                    strategy="non-retryable — skipping to escalation",
                    timestamp=time.time(),
                ))
                break

            is_retryable = isinstance(e, policy.retryable_exceptions)
            attempts.append(RecoveryAttempt(
                level=RecoveryLevel.RETRY,
                attempt_num=retry_num,
                error=str(e)[:500],
                strategy=f"retry {retry_num}/{policy.max_retries} (backoff)",
                timestamp=time.time(),
            ))

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

    _skip_to_escalation = last_error is not None and _is_non_retryable(last_error)

    # Extract QA hints for creative amendments
    if last_error is not None:
        _err_str = str(last_error)
        _hints_marker = "QA_HINTS: "
        _hints_idx = _err_str.find(_hints_marker)
        if _hints_idx >= 0:
            current_kwargs["_qa_hints"] = _err_str[_hints_idx + len(_hints_marker):]

    # ── Level 2: Creative amendment ───────────────────────────────────
    amendments = policy.creative_amendments or []
    if _skip_to_escalation:
        amendments = []
    for amend_num, amend_fn in enumerate(amendments[:policy.creative_budget], 1):
        try:
            current_kwargs = amend_fn(current_kwargs)
            result = operation(**current_kwargs)
            logger.info(
                "Recovery L2: '%s' succeeded with amendment %d",
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
                "Recovery L2: '%s' amendment %d (%s) failed: %s",
                operation_name, amend_num, amend_fn.__name__, str(e)[:200],
            )

    # ── Level 3: Environmental assessment ─────────────────────────────
    if not _skip_to_escalation and policy.enable_env_assessment and last_error is not None:
        logger.info("Recovery L3: '%s' — running environmental assessment", operation_name)
        diagnosis = _assessor.diagnose(last_error, operation_name, context)
        attempts.append(RecoveryAttempt(
            level=RecoveryLevel.COLLABORATIVE,
            attempt_num=1,
            error=str(last_error)[:500],
            strategy=f"environmental assessment: {diagnosis.root_cause}",
            timestamp=time.time(),
        ))

    # ── Level 4: Human escalation ─────────────────────────────────────
    return _escalate_to_human(
        operation, operation_name, current_kwargs, policy,
        last_error, attempts, diagnosis,
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


# ---------------------------------------------------------------------------
# Pipeline-level escalation helper (cross-cutting concern)
# ---------------------------------------------------------------------------

def escalate_pipeline_error(
    operation_name: str,
    error_msg: str,
    severity: str = "critical",
    proposed_actions: Optional[list[dict]] = None,
    default_action: str = "abort",
    diagnosis_hint: Optional[str] = None,
    wait_timeout_sec: float = 600.0,
    pipeline_state: Optional[dict] = None,
    diagnostic_data: Optional[dict] = None,
    agent_policy_type: Optional[str] = None,
) -> dict:
    """Cross-cutting recovery entry point with agent-powered ladder.

    This is the **cross-cutting concern** that every error site in the
    pipeline should use instead of ``raise RuntimeError(...)``.  It now
    runs the full agent-powered recovery ladder (L0–L3) before falling
    back to human escalation (L4):

    1. Select an agent policy based on ``agent_policy_type``
       ("audio", "video", "production", "otio", "generic").
    2. Run the agent ladder: L0 (domain fix) → L1 (intelligent retry)
       → L2 (creative) → L3 (collaborative/inter-agent).
    3. If all agent levels are exhausted:
       - Auto-approve mode: returns ``{"action": default_action}``
       - Manual mode: submits human escalation, waits for response.

    Args:
        operation_name: What failed (for logging + agent context).
        error_msg: The error message.
        severity: "warning" or "critical".
        proposed_actions: Human escalation actions (if agents fail).
        default_action: Auto-approve fallback action.
        diagnosis_hint: Extra context for the agents.
        wait_timeout_sec: Human escalation timeout.
        pipeline_state: Pipeline state dict (passed to agents).
        diagnostic_data: Domain-specific data (timing, QA results, etc.).
        agent_policy_type: Which agent set to use:
            "audio", "video", "production", "otio", "generic", or None.
            If None, runs agent ladder with generic agents (no L0).

    Returns:
        Response dict with at minimum ``{"action": "..."}``.
    """
    # ── Step 1: Run agent-powered ladder (L0–L3) ─────────────────────
    agent_decision = _run_agent_ladder(
        operation_name=operation_name,
        error_msg=error_msg,
        diagnosis_hint=diagnosis_hint,
        pipeline_state=pipeline_state,
        diagnostic_data=diagnostic_data,
        agent_policy_type=agent_policy_type,
    )

    if agent_decision is not None:
        action = agent_decision.get("action", "escalate")
        if action in ("fix", "retry", "skip"):
            # Map agent vocabulary to caller vocabulary so pipeline callers
            # (which check for "skip", "retry_with_fix", "amend") recognise
            # the agent's decision without raising RuntimeError.
            _AGENT_TO_CALLER = {"fix": "retry_with_fix", "retry": "retry_with_fix"}
            mapped = _AGENT_TO_CALLER.get(action, action)
            agent_decision["action"] = mapped
            logger.info(
                "Agent ladder resolved '%s' at L%s: action=%s (agent=%s)",
                operation_name,
                agent_decision.get("level", "?"),
                mapped,
                action,
            )
            return agent_decision
        elif action == "abort":
            logger.warning(
                "Agent ladder decided to abort '%s' at L%s (agent=%s)",
                operation_name,
                agent_decision.get("level", "?"),
                agent_decision.get("agent", "?"),
            )
            return {"action": "abort", **agent_decision}

    # ── Step 1.5: Consult the Production Supervisor LLM ──────────────
    # The agent ladder returned "escalate" (or no agents were available).
    # Before falling through to human intervention, the supervisor MUST
    # make at least one LLM call — this is the hard invariant for #102.
    # Closes #61 (ladder passes buck), #73 (zero LLM reasoning),
    # #77 (inter-agent communication via supervisor).
    supervisor_decision = _consult_supervisor(
        operation_name=operation_name,
        error_msg=error_msg,
        diagnosis_hint=diagnosis_hint,
        pipeline_state=pipeline_state,
        diagnostic_data=diagnostic_data,
        agent_chain=(agent_decision or {}).get("agent_chain"),
    )
    if supervisor_decision is not None:
        logger.info(
            "Supervisor LLM resolved '%s': caller_action=%s (canonical=%s, level=L%s)",
            operation_name,
            supervisor_decision.get("action"),
            supervisor_decision.get("canonical_action"),
            supervisor_decision.get("level"),
        )
        return supervisor_decision

    # ── Step 2: Agent ladder exhausted — fall back to human (L4) ─────
    if proposed_actions is None:
        proposed_actions = [
            {
                "action_id": "retry_with_fix",
                "description": "Re-run the failing stage after addressing the root cause",
                "risk_level": "low",
            },
            {
                "action_id": "skip",
                "description": f"Skip {operation_name} and continue the pipeline",
                "risk_level": "medium",
            },
            {
                "action_id": "abort",
                "description": "Abort the pipeline",
                "risk_level": "high",
            },
        ]

    escalation_id = _next_escalation_id()

    # Include agent chain in the diagnosis
    agent_chain_summary = ""
    if agent_decision and agent_decision.get("agent_chain"):
        chain = agent_decision["agent_chain"]
        agent_chain_summary = (
            f" Agent chain ({len(chain)} attempts): "
            + "; ".join(
                f"L{a.get('level', '?')} {a.get('agent', '?')}: {a.get('action', '?')}"
                for a in chain[-5:]
            )
        )

    diag_dict = {
        "root_cause": (diagnosis_hint or error_msg[:300]) + agent_chain_summary,
        "confidence": "likely",
        "proposed_fix": f"All agent levels exhausted for {operation_name}. "
                        f"Manual review needed.",
    }

    req = HumanEscalationRequest(
        id=escalation_id,
        operation_name=operation_name,
        error_chain=[{
            "level": "PIPELINE",
            "error": error_msg[:500],
            "strategy": "escalate_pipeline_error (agents exhausted)",
            "timestamp": time.time(),
        }],
        diagnosis=diag_dict,
        proposed_actions=proposed_actions,
        severity=severity,
        timestamp=time.time(),
    )
    submit_escalation(req)

    # Auto-approve mode
    auto_approve = os.environ.get(
        "DOCUMENTARY_AUTO_APPROVE", ""
    ).strip().lower() in ("1", "true", "yes")
    if auto_approve:
        logger.warning(
            "Escalation %s auto-resolved (auto-approve mode): %s → %s",
            escalation_id, operation_name, default_action,
        )
        resolve_escalation(escalation_id, {
            "action": default_action,
            "comment": "auto-resolved (DOCUMENTARY_AUTO_APPROVE) — agents exhausted",
            "timestamp": time.time(),
        })
        return {"action": default_action}

    # Manual mode — wait for human
    logger.critical(
        "ESCALATION %s: '%s' — agents exhausted, waiting for human (timeout %.0fs). "
        "Error: %s",
        escalation_id, operation_name, wait_timeout_sec, error_msg[:200],
    )
    human_response = _wait_for_human_response(escalation_id, wait_timeout_sec)

    if human_response:
        return human_response

    logger.error(
        "Escalation %s timed out — defaulting to abort for '%s'",
        escalation_id, operation_name,
    )
    return {"action": "abort", "comment": "timeout — no human response"}


def _run_agent_ladder(
    operation_name: str,
    error_msg: str,
    diagnosis_hint: Optional[str] = None,
    pipeline_state: Optional[dict] = None,
    diagnostic_data: Optional[dict] = None,
    agent_policy_type: Optional[str] = None,
) -> Optional[dict]:
    """Run the agent-powered recovery ladder (L0–L3) and return the decision.

    Returns None if all agent levels are exhausted.
    Returns a dict with {"action": ..., "state_patches": ..., "level": ..., "agent_chain": [...]}
    """
    # Select agent policy
    policy_factories = {
        "audio": _make_audio_agent_policy,
        "video": _make_video_agent_policy,
        "production": _make_production_agent_policy,
        "otio": _make_otio_agent_policy,
        "generic": _make_generic_agent_policy,
    }

    factory = policy_factories.get(agent_policy_type or "generic")
    if factory is None:
        factory = _make_generic_agent_policy

    try:
        policy = factory()
    except ImportError:
        logger.warning(
            "Recovery agents not available (ImportError) — skipping agent ladder for '%s'",
            operation_name,
        )
        return None

    if not policy.agents:
        return None

    from recovery_agents import RecoveryContext

    agent_chain: list[dict] = []
    _level_names = {
        0: "FIX (domain specialist)",
        1: "RETRY (intelligent retry)",
        2: "CREATIVE (alternative strategy)",
        3: "COLLABORATIVE (inter-agent coordination)",
    }

    for level in range(4):
        agent = policy.get_agent(level)
        if agent is None:
            continue

        budget = policy.get_level_budget(level)
        level_name = _level_names.get(level, f"Level {level}")

        logger.info(
            "Agent ladder L%d (%s): '%s' — agent '%s' (budget: %d)",
            level, level_name, operation_name, agent.name, budget,
        )

        for attempt_num in range(1, budget + 1):
            ctx = RecoveryContext(
                operation_name=operation_name,
                error_msg=error_msg,
                current_level=level,
                level_name=level_name,
                attempt_num=attempt_num,
                max_attempts=budget,
                previous_attempts=agent_chain.copy(),
                pipeline_state=pipeline_state or {},
                diagnostic_data=diagnostic_data or {},
            )

            try:
                decision = agent.decide(ctx)
            except Exception as agent_err:
                logger.error(
                    "Agent ladder L%d: agent '%s' crashed: %s",
                    level, agent.name, str(agent_err)[:300],
                )
                agent_chain.append({
                    "level": level,
                    "agent": agent.name,
                    "action": "error",
                    "explanation": f"Agent crashed: {agent_err}",
                })
                break

            agent_chain.append({
                "level": level,
                "agent": agent.name,
                "action": decision.action,
                "explanation": decision.explanation[:300],
                "state_patches": decision.state_patches,
                "confidence": decision.confidence,
            })

            logger.info(
                "Agent ladder L%d: '%s' decided action=%s (confidence=%.2f): %s",
                level, agent.name, decision.action,
                decision.confidence, decision.explanation[:200],
            )

            if decision.action in ("fix", "retry"):
                return {
                    "action": decision.action,
                    "state_patches": decision.state_patches,
                    "level": level,
                    "agent": agent.name,
                    "explanation": decision.explanation,
                    "agent_chain": agent_chain,
                }
            elif decision.action == "skip":
                return {
                    "action": "skip",
                    "level": level,
                    "agent": agent.name,
                    "explanation": decision.explanation,
                    "agent_chain": agent_chain,
                }
            elif decision.action == "abort":
                return {
                    "action": "abort",
                    "level": level,
                    "agent": agent.name,
                    "explanation": decision.explanation,
                    "agent_chain": agent_chain,
                }
            elif decision.action == "escalate":
                break  # Move to next level

    # All agent levels exhausted
    logger.warning(
        "Agent ladder exhausted for '%s' after %d attempts across %d levels",
        operation_name, len(agent_chain),
        len(set(a["level"] for a in agent_chain)),
    )
    return {"action": "escalate", "agent_chain": agent_chain}


# ---------------------------------------------------------------------------
# Production Supervisor consultation (bridges agent ladder to canonical menu)
# ---------------------------------------------------------------------------
#
# Closes #61, #73, #76, #77, #102, #103: when the agent ladder returns
# "escalate" we consult the Production Supervisor via
# ``supervisor_escalate(context) -> EscalationAction``.  This GUARANTEES
# at least one supervisor LLM call per escalation, which is the hard
# invariant asserted at end-of-run by
# ``agents.production_supervisor.assert_supervisor_invariant_at_end_of_run``.

# Canonical EscalationAction → caller-vocabulary mapping used by
# escalate_pipeline_error's callers (video_tools, audio_tools, otio_tools,
# orchestrator).
_CANONICAL_TO_CALLER: dict[str, str] = {
    "regenerate_clip": "retry_with_fix",
    "generate_extension_clip": "retry_with_fix",
    "speed_up_narration": "retry_with_fix",
    "trim_narration": "retry_with_fix",
    "freeze_frame_fill": "retry_with_fix",
    "replace_with_brand_card": "skip",
    "rewrite_scene": "retry_with_fix",
    "abort_run": "abort",
}


def _consult_supervisor(
    operation_name: str,
    error_msg: str,
    diagnosis_hint: Optional[str] = None,
    pipeline_state: Optional[dict] = None,
    diagnostic_data: Optional[dict] = None,
    agent_chain: Optional[list[dict]] = None,
) -> Optional[dict]:
    """Consult the production supervisor for a canonical escalation action.

    Returns a dict in the caller's vocabulary (``{"action": ..., ...}``)
    or ``None`` if the supervisor cannot be consulted (e.g. module not
    importable in a minimal test harness).

    Notes:
        - The supervisor ALWAYS makes at least one LLM call per invocation
          — even parse failures increment the counter — so the #102
          invariant is satisfied by construction.
        - On terminal parse failure the supervisor returns ``abort_run``,
          which we map to the caller's ``abort``.
    """
    try:
        from agents.production_supervisor import (
            supervisor_escalate,
        )
        from orchestrator.escalation_menu import EscalationContext
    except Exception as exc:  # pragma: no cover — import-time safety net
        logger.warning(
            "Supervisor unavailable for '%s': %s — skipping supervisor layer",
            operation_name, exc,
        )
        return None

    # Infer high-cost from diagnostic data (e.g. long clips, rewrites).
    high_cost = False
    if diagnostic_data:
        dur = diagnostic_data.get("duration") or diagnostic_data.get("duration_needed")
        if isinstance(dur, (int, float)) and dur >= 8.0:
            high_cost = True
        if diagnostic_data.get("high_cost"):
            high_cost = True

    descriptor: dict[str, Any] = {
        "error": error_msg[:500],
        "diagnosis_hint": diagnosis_hint or "",
    }
    if diagnostic_data:
        descriptor.update(
            {k: v for k, v in diagnostic_data.items() if _is_jsonable(v)}
        )

    history: list[dict[str, Any]] = []
    for attempt in (agent_chain or [])[-10:]:
        history.append({
            "action": f"L{attempt.get('level', '?')}:{attempt.get('action', '?')}",
            "outcome": attempt.get("explanation", "")[:200],
            "timestamp": time.time(),
        })

    context = EscalationContext(
        failing_artifact=operation_name,
        artifact_descriptor=descriptor,
        timeline_state_snapshot=(pipeline_state or {}),
        user_original_prompt=(pipeline_state or {}).get("user_prompt", "")
            if isinstance(pipeline_state, dict) else "",
        budget_remaining=0.0,
        escalation_history=history,
        high_cost=high_cost,
    )

    try:
        action = supervisor_escalate(context)
    except Exception as exc:
        logger.error(
            "Supervisor escalate raised for '%s': %s — falling through to human",
            operation_name, exc,
        )
        return None

    caller_action = _CANONICAL_TO_CALLER.get(action.action, "abort")
    return {
        "action": caller_action,
        "canonical_action": action.action,
        "level": action.level,
        "supervisor_reasoning": action.llm_reasoning,
        "supervisor_model": action.llm_model,
        "state_patches": action.to_dict(),
        "agent": "production_supervisor",
    }


def _is_jsonable(value: Any) -> bool:
    """Cheap check — is ``value`` safe to include in the supervisor prompt?"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_jsonable(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_jsonable(v) for k, v in value.items())
    return False
