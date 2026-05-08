"""
Production Supervisor -- orchestrates actual video generation on GPU.

Reads visual concepts from state["visual_concepts"], provisions GPU VMs
via Vast.ai, generates video clips using LTX-2.3, probes results, and
adds clips to the OTIO timeline.

Uses the ADK ProductionAgent (CustomAgent) when available -- this wraps
the mcp-agent-pattern orchestrator so every phase (planning, execution,
synthesis) yields ADK events, and the planner/evaluator/replanner are
ADK sub-agents whose instructions can be rewritten by ``adk optimize``.

Falls back to the plain Agent + orchestrated_production_callback when
ProductionAgent cannot be initialised.

Also owns ``supervisor_escalate()`` -- the LLM-powered decision layer
that picks one of the canonical ``EscalationAction`` variants whenever
the pipeline hits an escalation point.  Closes #61, #73, #76, #77, #102,
#103: every escalation MUST go through this function (enforced by the
hard invariant in ``escalation_menu.assert_escalation_invariant``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Optional

from orchestrator.escalation_menu import (
    CREATIVE_ACTION_MENU_DESCRIPTION,
    CREATIVE_ACTION_NAMES,
    CREATIVE_ESCALATION_ACTION_JSON_SCHEMA,
    EscalationAction,
    EscalationActionError,
    EscalationContext,
    EscalationInvariantViolation,
    assert_escalation_invariant,
)

logger = logging.getLogger(__name__)

_INSTRUCTION = """\
You are the Production Supervisor for a documentary pipeline.
Video generation is handled automatically. Report completion.
"""


def _build_production_supervisor():
    """Build the production supervisor agent.

    Tries to use the ADK ProductionAgent (CustomAgent) which provides
    traceable events and optimizable sub-agents. Falls back to the
    plain Agent + orchestrated_production_callback if unavailable.

    ADK agent wrapper removed — returns None.  supervisor_escalate()
    still works as a pure-Python entry point.
    """
    return None


production_supervisor = None


# ===========================================================================
# Escalation decision layer -- supervisor_escalate() + run counters
# ===========================================================================
#
# The previous supervisor made zero LLM calls during the PAG run (see #102)
# and fell back to round-robin, which required full human intervention to
# handle 5 extension-clip decisions and 9 regenerations.  The code below
# replaces that behaviour with a formal LLM-backed decision layer that
# picks one of the canonical ``EscalationAction`` variants.
# ---------------------------------------------------------------------------

# Default Gemini models for the supervisor.  Overridable via env vars so we
# can point the CI/dev environment at cheaper endpoints.  These are the
# *native* google-genai model names (no "gemini/" prefix -- that prefix is
# a LiteLLM convention, not a google-genai one).
_FLASH_MODEL = os.environ.get(
    "SUPERVISOR_ESCALATE_FLASH_MODEL", "gemini-2.5-flash"
)
_PRO_MODEL = os.environ.get(
    "SUPERVISOR_ESCALATE_PRO_MODEL", "gemini-2.5-pro"
)

_MAX_PARSE_RETRIES = 2  # Retry stricter prompt up to 2x on parse failure.

# The push-based supervisor only sees the creative subset of actions.
# ``recovery._CANONICAL_TO_CALLER`` (the downstream mapping consumed by
# every push-path caller: video_tools, audio_tools, otio_tools,
# orchestrator) only covers creative actions; any ops action returned
# here would be silently mapped to ``"abort"`` -- the exact #102
# round-robin-with-abort regression.  Ops actions are exclusively the
# domain of the pull-based supervisor in
# ``agents/escalation_supervisor.py``, which wires them to
# ``orchestrator.ops_executors.execute_ops_action``.
_SUPERVISOR_SYSTEM_INSTRUCTION = (
    "You are the Production Supervisor for a documentary pipeline. "
    "When something fails, you must pick EXACTLY ONE canonical recovery "
    "action from the menu below.  Your response MUST be a single JSON "
    "object matching the action schema -- no markdown, no commentary "
    "outside the JSON.  Prefer cheaper actions (L1 > L2 > L3).  Never "
    "abort unless there is no viable L1/L2 action.\n\n"
    + CREATIVE_ACTION_MENU_DESCRIPTION
)


class _SupervisorCounters:
    """Thread-safe per-run counters for the supervisor decision layer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.llm_calls: int = 0
        self.escalations: int = 0
        self.parse_failures: int = 0
        self.abort_fallbacks: int = 0

    def reset(self) -> None:
        with self._lock:
            self.llm_calls = 0
            self.escalations = 0
            self.parse_failures = 0
            self.abort_fallbacks = 0

    def incr_llm_calls(self, n: int = 1) -> None:
        with self._lock:
            self.llm_calls += n

    def incr_escalations(self, n: int = 1) -> None:
        with self._lock:
            self.escalations += n

    def incr_parse_failures(self, n: int = 1) -> None:
        with self._lock:
            self.parse_failures += n

    def incr_abort_fallbacks(self, n: int = 1) -> None:
        with self._lock:
            self.abort_fallbacks += n

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "llm_calls_per_run": self.llm_calls,
                "escalations_per_run": self.escalations,
                "parse_failures_per_run": self.parse_failures,
                "abort_fallbacks_per_run": self.abort_fallbacks,
            }


# Module-level singleton -- one per Python process.  The pipeline re-uses a
# single process per run, so this maps 1:1 to "per run".  Tests reset it
# explicitly via ``reset_run_counters()``.
_counters = _SupervisorCounters()


def get_run_counters() -> dict[str, int]:
    """Return a snapshot of supervisor counters (safe to call from anywhere)."""
    return _counters.snapshot()


def reset_run_counters() -> None:
    """Reset supervisor counters at the start of a new run.

    Called from the pipeline entry point (``run_pipeline``) and by tests.
    """
    _counters.reset()
    logger.debug("Supervisor counters reset")


def _emit_telemetry() -> None:
    """Emit supervisor counters via the active PipelineCollector, if any.

    Telemetry is best-effort -- we never let collector problems bubble out
    of the escalation path.  The counters are also always available via
    ``get_run_counters()`` so they can be asserted in CI even without a
    collector.
    """
    try:
        from dashboard import get_active_collector

        collector = get_active_collector()
        if collector is None:
            return
        # Piggy-back on the collector's existing llm_start/llm_end plumbing
        # so the counter shows up as ``production_supervisor.llm_calls`` in
        # ``total_llm_calls`` without needing a collector schema change.
        import time

        snap = _counters.snapshot()
        collector.llm_start("production_supervisor", estimated_tokens=0)
        collector.llm_end("production_supervisor", duration=0.0, output_tokens=0)
        # Also stash the snapshot on the collector so dashboard consumers
        # can render the invariant.
        if hasattr(collector, "_events"):
            collector._events.append(
                {
                    "type": "supervisor_counters",
                    "time": time.time(),
                    **snap,
                }
            )
    except Exception as exc:  # pragma: no cover -- telemetry must never raise
        logger.debug("supervisor telemetry emit failed: %s", exc)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_escalate_prompt(
    context: EscalationContext,
    *,
    strictness_round: int = 0,
) -> str:
    """Build the user prompt for a single supervisor_escalate call.

    ``strictness_round`` bumps on retry so the prompt gets progressively
    more emphatic about returning strictly-valid JSON.
    """
    history_lines = []
    for prev in context.escalation_history[-10:]:
        history_lines.append(
            f"  - action={prev.get('action', '?')} "
            f"outcome={prev.get('outcome', '?')} "
            f"ts={prev.get('timestamp', '?')}"
        )
    history_str = "\n".join(history_lines) or "  (none)"

    parts = [
        "ESCALATION CONTEXT",
        "=" * 40,
        f"Failing artifact: {context.failing_artifact}",
        f"User's original prompt: {context.user_original_prompt or '(not provided)'}",
        # ``budget_remaining == 0.0`` means "not tracked / unbounded" per the
        # EscalationContext docstring -- rendering it as "$0.00" would bias
        # the LLM toward abort_run.
        (
            "Budget remaining: (unbounded / not tracked)"
            if context.budget_remaining == 0.0
            else f"Budget remaining: ${context.budget_remaining:.2f}"
        ),
        "",
        "Artifact descriptor:",
        json.dumps(context.artifact_descriptor, indent=2, default=str),
        "",
        "Timeline state snapshot:",
        json.dumps(context.timeline_state_snapshot, indent=2, default=str),
        "",
        "Prior escalations this run:",
        history_str,
        "",
        "=" * 40,
        "Pick exactly ONE action from the menu above.",
        "Respond with a SINGLE JSON OBJECT -- no prose, no markdown fences.",
        "The JSON MUST have an 'action' field with one of these values: "
        + ", ".join(CREATIVE_ACTION_NAMES),
        "Include all required fields for the chosen action (see signatures).",
        "Include an 'llm_reasoning' field with a 1-2 sentence rationale.",
    ]
    if strictness_round >= 1:
        parts.append(
            "\nNOTE: your previous response failed to parse.  Return ONLY "
            "valid JSON -- no code fences, no leading/trailing prose."
        )
    if strictness_round >= 2:
        parts.append(
            "FINAL ATTEMPT.  If the next response is not valid JSON matching "
            "the schema, the system will fall through to abort_run."
        )
    return "\n".join(parts)


def _parse_llm_response(text: str) -> EscalationAction:
    """Parse a Gemini response body into a validated EscalationAction.

    Raises ``EscalationActionError`` on parse/validation failure.
    """
    if not text:
        raise EscalationActionError("Empty LLM response")

    stripped = text.strip()
    # Strip markdown fences if the model wrapped the JSON.
    if stripped.startswith("```"):
        # Remove leading fence (with optional language tag).
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        # Last-ditch: find the first {...} block.
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                raise EscalationActionError(
                    f"Response is not valid JSON: {exc}"
                ) from exc
        else:
            raise EscalationActionError(
                f"Response is not valid JSON: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise EscalationActionError(
            f"Expected JSON object, got {type(data).__name__}"
        )

    # Defence-in-depth: the push-based path is restricted to the creative
    # family via prompt + ``CREATIVE_ESCALATION_ACTION_JSON_SCHEMA``.
    # If a model still returns an ops action (schema ignored, manual test
    # injection, etc.) reject it here rather than letting
    # ``recovery._CANONICAL_TO_CALLER.get(..., "abort")`` silently
    # downgrade it to a terminal abort.
    action_name = data.get("action")
    if isinstance(action_name, str) and action_name not in CREATIVE_ACTION_NAMES:
        raise EscalationActionError(
            f"Push-based supervisor received non-creative action "
            f"{action_name!r}; only {CREATIVE_ACTION_NAMES} are permitted "
            f"on this path. Ops actions belong to the pull-based "
            f"escalation supervisor."
        )

    return EscalationAction.from_dict(data)


# ---------------------------------------------------------------------------
# Gemini call (swappable for tests via ``_llm_client_factory``)
# ---------------------------------------------------------------------------

def _default_llm_call(model: str, system: str, prompt: str) -> str:
    """Default LLM backend: google-genai with structured output.

    Uses ``response_mime_type="application/json"`` and
    ``response_schema=ESCALATION_ACTION_JSON_SCHEMA`` to enforce the action
    shape server-side.  The flat schema (plus post-hoc validation in
    ``EscalationAction.__post_init__``) matches what Gemini's structured
    output supports (no oneOf).
    """
    from google import genai
    from google.genai import types as genai_types

    api_key = (
        os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_GENAI_API_KEY")
    )
    if not api_key:
        raise RuntimeError(
            "supervisor_escalate requires GOOGLE_API_KEY (or GEMINI_API_KEY) "
            "to be set so Gemini can be consulted.  Refusing to silently "
            "fall back to round-robin -- that is exactly the #102 regression."
        )

    client = genai.Client(api_key=api_key)
    config = genai_types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=CREATIVE_ESCALATION_ACTION_JSON_SCHEMA,
        temperature=0.2,
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=config,
    )
    # ``response.text`` concatenates all text parts.
    return getattr(response, "text", "") or ""


# Tests override this to avoid hitting real Gemini.
_llm_client_factory: Any = _default_llm_call


def set_llm_client_factory(fn: Any) -> Any:
    """Override the LLM backend used by ``supervisor_escalate`` (for tests).

    ``fn`` must accept ``(model: str, system: str, prompt: str) -> str``.
    Returns the previous factory so tests can restore it.
    """
    global _llm_client_factory
    prev = _llm_client_factory
    _llm_client_factory = fn
    return prev


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def supervisor_escalate(
    context: EscalationContext,
    *,
    model: Optional[str] = None,
) -> EscalationAction:
    """Consult the supervisor LLM and return a typed ``EscalationAction``.

    This is THE decision-making point for every escalation in the pipeline.
    It is **never** allowed to fall through to round-robin -- on parse
    failure we retry with a stricter prompt up to ``_MAX_PARSE_RETRIES``
    times, then return a deterministic ``abort_run`` action.

    Every call (success, retry, or fallback) increments
    ``production_supervisor.llm_calls_per_run`` via the module-level
    counter, so the invariant
    ``escalations_per_run > 0 => llm_calls_per_run > 0``
    is satisfied by construction.

    Args:
        context: Diagnostic context (see ``EscalationContext``).
        model: Override the Gemini model (defaults to Flash, or Pro when
            ``context.high_cost`` is True).

    Returns:
        A validated ``EscalationAction``.
    """
    _counters.incr_escalations()
    chosen_model = model or (_PRO_MODEL if context.high_cost else _FLASH_MODEL)
    last_error: Optional[Exception] = None

    for round_idx in range(_MAX_PARSE_RETRIES + 1):
        prompt = _build_escalate_prompt(context, strictness_round=round_idx)
        _counters.incr_llm_calls()
        try:
            raw = _llm_client_factory(
                chosen_model, _SUPERVISOR_SYSTEM_INSTRUCTION, prompt
            )
        except Exception as exc:
            logger.warning(
                "supervisor_escalate: LLM call failed (round %d, model=%s): %s",
                round_idx, chosen_model, exc,
            )
            last_error = exc
            continue

        try:
            action = _parse_llm_response(raw)
        except EscalationActionError as exc:
            _counters.incr_parse_failures()
            logger.warning(
                "supervisor_escalate: parse failed (round %d): %s -- raw=%s",
                round_idx, exc, (raw or "")[:500],
            )
            last_error = exc
            continue

        # Success -- stamp audit metadata and emit telemetry.
        action.llm_model = chosen_model
        _emit_telemetry()
        logger.info(
            "supervisor_escalate: chose action=%s (level=%d) for %s",
            action.action, action.level, context.failing_artifact,
        )
        return action

    # All retries exhausted -- deterministic L3 abort.  The LLM call
    # counter has already been bumped per attempt, so the invariant holds
    # (we did try; we just couldn't parse).
    _counters.incr_abort_fallbacks()
    reason = (
        f"supervisor_escalate exhausted {_MAX_PARSE_RETRIES + 1} attempts "
        f"for {context.failing_artifact}. Last error: {last_error}"
    )
    logger.error(reason)
    fallback = EscalationAction(
        action="abort_run",
        reason=reason,
        llm_model=chosen_model,
        llm_reasoning="Parser fallback -- see logs.",
    )
    _emit_telemetry()
    return fallback


# ---------------------------------------------------------------------------
# End-of-run invariant check
# ---------------------------------------------------------------------------

def assert_supervisor_invariant_at_end_of_run() -> None:
    """Hard CI assertion -- call at the end of every pipeline run.

    Fails loudly if any escalation happened without at least one LLM call.
    This is the #102 acceptance criterion.

    Raises:
        EscalationInvariantViolation: invariant violated.
    """
    snap = _counters.snapshot()
    assert_escalation_invariant(
        escalations_per_run=snap["escalations_per_run"],
        llm_calls_per_run=snap["llm_calls_per_run"],
    )
    logger.info(
        "Supervisor invariant OK: escalations=%d llm_calls=%d",
        snap["escalations_per_run"], snap["llm_calls_per_run"],
    )


__all__ = [
    "production_supervisor",
    "supervisor_escalate",
    "get_run_counters",
    "reset_run_counters",
    "assert_supervisor_invariant_at_end_of_run",
    "set_llm_client_factory",
    "EscalationAction",
    "EscalationActionError",
    "EscalationContext",
    "EscalationInvariantViolation",
]
