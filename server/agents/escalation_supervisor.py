"""Pull-based escalation supervisor (PR-2).

The legacy :func:`agents.production_supervisor.supervisor_escalate` is
push-based: the caller hand-packs an :class:`orchestrator.escalation_menu.EscalationContext`
with everything it *thinks* the supervisor might need, then the supervisor
makes a single JSON-structured LLM call to pick an action.

PR-2 introduces the pull-based counterpart: the supervisor receives a
minimal :class:`orchestrator.escalation_scope.EscalationScope` plus a set
of read-tools (from :mod:`orchestrator.escalation_tools`) and decides
*itself* what context to fetch before picking an action.  This is a
strictly additive entry point --- it does not modify
``supervisor_escalate`` or its existing call-sites; recovery.py opts in
via :func:`route_context_through_scope`.

Why pull-based:

* Cheap cases stay cheap.  A critic-reject on a single clip resolves
  without fetching fleet / cost / timeline state.
* Context is fresh at decision time rather than snapshotted upstream.
* New context sources become new read-tools, not new ``EscalationContext``
  fields -- no churn across every call site.
* The tool-call log becomes a self-documenting audit trail.

Both creative and ops actions are available; the supervisor prompt
(assembled from :data:`orchestrator.escalation_menu.ACTION_MENU_DESCRIPTION`)
already describes both families.

The LLM backend is dependency-injected behind
:func:`set_supervisor_runner` so tests can replace it with a deterministic
fake that exercises the read-tool dispatch + action recording without
needing Gemini.  The default runner uses ``google.genai`` function
calling; if the SDK / API key aren't available the supervisor falls
back to :func:`agents.production_supervisor.supervisor_escalate` with a
synthesised push-context so we never silently lose the decision layer.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from critique.record import EscalationRef
from critique.store import ArtifactCritiqueStore, get_critique_store
from orchestrator.escalation_menu import (
    ACTION_MENU_DESCRIPTION,
    ACTION_NAMES,
    ESCALATION_ACTION_JSON_SCHEMA,
    EscalationAction,
    EscalationActionError,
)
from orchestrator.escalation_scope import EscalationScope
from orchestrator import escalation_tools as _tools

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Read-tool registry exposed to the supervisor LLM
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReadToolSpec:
    """Declarative description of a read-only tool callable."""

    name: str
    description: str
    parameters: dict[str, Any]   # JSON schema fragment
    fn: Callable[..., Any]


def _make_read_tools() -> list[ReadToolSpec]:
    """Wrap the read-only functions in :mod:`orchestrator.escalation_tools`.

    Every tool is pure Python; no LLM inside, no state mutation, and
    never raises (the underlying functions are duck-typed + guarded).
    """
    return [
        ReadToolSpec(
            name="read_artifact_critique_history",
            description=(
                "Return the ArtifactCritiqueRecord for a specific artifact "
                "as a dict (critiques, qa_results, escalations).  Use when "
                "the scope has a primary_artifact_id."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "artifact_type": {
                        "type": "string",
                        "enum": ["scene", "visual_concept", "clip", "audio", "assembly"],
                    },
                    "artifact_id": {"type": "string"},
                },
                "required": ["artifact_type", "artifact_id"],
            },
            fn=lambda artifact_type, artifact_id: _tools.read_artifact_critique_history(
                artifact_id=artifact_id, artifact_type=artifact_type
            ),
        ),
        ReadToolSpec(
            name="read_qa_verdicts",
            description=(
                "Return the QA verdicts (jury, gatekeeper, timeline_guardian, "
                "etc.) for an artifact.  Cheaper than pulling the full record."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "artifact_type": {"type": "string"},
                    "artifact_id": {"type": "string"},
                },
                "required": ["artifact_type", "artifact_id"],
            },
            fn=lambda artifact_type, artifact_id: _tools.read_qa_verdicts(
                artifact_id=artifact_id, artifact_type=artifact_type
            ),
        ),
        ReadToolSpec(
            name="read_escalation_history",
            description=(
                "Return prior EscalationRef entries for an artifact (or all "
                "artifacts of a type if artifact_id is omitted)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "artifact_type": {"type": "string"},
                    "artifact_id": {"type": "string"},
                },
                "required": ["artifact_type"],
            },
            fn=lambda artifact_type, artifact_id=None: _tools.read_escalation_history(
                artifact_id=artifact_id, artifact_type=artifact_type
            ),
        ),
        ReadToolSpec(
            name="read_worker_health",
            description=(
                "Return per-worker snapshots from infra_agent: status, vram, "
                "consecutive failures, timing.  Optional role filter (tts / "
                "video / whisperx)."
            ),
            parameters={
                "type": "object",
                "properties": {"role": {"type": "string"}},
            },
            fn=lambda role=None: _tools.read_worker_health(role),
        ),
        ReadToolSpec(
            name="read_stage_timing",
            description=(
                "Return current stage timing: started_at, expected, observed, "
                "over-budget flag. Useful for stage_timeout scopes."
            ),
            parameters={"type": "object", "properties": {}},
            fn=lambda: _tools.read_stage_timing(),
        ),
        ReadToolSpec(
            name="read_infra_status_snapshot",
            description=(
                "Return the full infra_agent status (fleet summary + paused "
                "state + recent escalations).  Use for fleet-wide decisions."
            ),
            parameters={"type": "object", "properties": {}},
            fn=lambda: _tools.read_infra_status_snapshot(),
        ),
        ReadToolSpec(
            name="read_infra_escalation_log",
            description=(
                "Return the last N infra-agent escalation events (worker "
                "unreachable, stage slow, etc.)."
            ),
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
            fn=lambda limit=20: _tools.read_infra_escalation_log(limit),
        ),
        ReadToolSpec(
            name="read_vast_cost_snapshot",
            description=(
                "Return vast.ai cost snapshot: per_hour, instances, "
                "budget_remaining.  Use for cost_exceeded scopes."
            ),
            parameters={"type": "object", "properties": {}},
            fn=lambda: _tools.read_vast_cost_snapshot(),
        ),
        ReadToolSpec(
            name="read_timeline_state",
            description=(
                "Return the pipeline timeline state snapshot (durations, "
                "gaps, per-scene completion) from the most recent B2 "
                "checkpoint."
            ),
            parameters={"type": "object", "properties": {}},
            fn=lambda: _tools.read_timeline_state(),
        ),
        ReadToolSpec(
            name="read_artifact_record",
            description=(
                "Return the full ArtifactCritiqueRecord for an artifact "
                "(critiques + QA verdicts + escalations).  Heavier than the "
                "targeted read_* tools; use when you need everything."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "artifact_type": {"type": "string"},
                    "artifact_id": {"type": "string"},
                },
                "required": ["artifact_type", "artifact_id"],
            },
            fn=lambda artifact_type, artifact_id: (
                _tools.read_artifact_record(
                    artifact_id=artifact_id, artifact_type=artifact_type
                )
                or {}
            ),
        ),
    ]


READ_TOOLS: list[ReadToolSpec] = _make_read_tools()
READ_TOOLS_BY_NAME: dict[str, ReadToolSpec] = {t.name: t for t in READ_TOOLS}


# ---------------------------------------------------------------------------
# Supervisor runner: pluggable backend
# ---------------------------------------------------------------------------

# A SupervisorRunner decides an action given (scope, system prompt, tools).
# It is responsible for:
#  * calling the LLM (or a fake),
#  * dispatching tool calls against the injected tool registry,
#  * returning a validated EscalationAction.
SupervisorRunner = Callable[
    [EscalationScope, str, dict[str, ReadToolSpec], str],
    EscalationAction,
]

_supervisor_runner: Optional[SupervisorRunner] = None
_runner_lock = threading.Lock()


def set_supervisor_runner(runner: Optional[SupervisorRunner]) -> Optional[SupervisorRunner]:
    """Override the supervisor LLM backend (for tests).

    Returns the previous runner so tests can restore it after the
    override.  Passing ``None`` resets to the default backend.
    """
    global _supervisor_runner
    with _runner_lock:
        prev = _supervisor_runner
        _supervisor_runner = runner
    return prev


# ---------------------------------------------------------------------------
# Counters (mirror the invariant on the new path too)
# ---------------------------------------------------------------------------

class _ScopeCounters:
    """Thread-safe counters for the pull-based supervisor path."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.escalations: int = 0
        self.llm_calls: int = 0
        self.tool_calls: int = 0
        self.fallback_decisions: int = 0
        self.recorded_refs: int = 0

    def reset(self) -> None:
        with self._lock:
            self.escalations = 0
            self.llm_calls = 0
            self.tool_calls = 0
            self.fallback_decisions = 0
            self.recorded_refs = 0

    def incr(self, field: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + n)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "escalations_per_run": self.escalations,
                "llm_calls_per_run": self.llm_calls,
                "tool_calls_per_run": self.tool_calls,
                "fallback_decisions_per_run": self.fallback_decisions,
                "recorded_refs_per_run": self.recorded_refs,
            }


_counters = _ScopeCounters()


def get_scope_counters() -> dict[str, int]:
    return _counters.snapshot()


def reset_scope_counters() -> None:
    _counters.reset()


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTION = (
    "You are the pull-based Production Supervisor.  You receive a scope "
    "(a minimal description of the failure) and a set of READ-ONLY tools "
    "for pulling the context you need.  Call the smallest set of tools "
    "that lets you choose the right canonical action, then return a "
    "single EscalationAction JSON matching the schema.\n"
    "\n"
    "Creative actions mutate artifacts (clips, scenes, narration). "
    "Ops actions mutate the fleet (workers, provisioning, batch state). "
    "Read worker health / stage timing / cost before picking ops actions; "
    "read artifact critique history / qa verdicts before picking "
    "creative actions.  Prefer the cheapest action (L1 > L2 > L3) that "
    "actually resolves the root cause.\n"
    "\n"
    + ACTION_MENU_DESCRIPTION
)


def build_user_prompt(scope: EscalationScope) -> str:
    """Assemble the supervisor user prompt for a scope.

    Kept small and deterministic so prompt diffs stay reviewable.
    """
    return (
        f"{scope.to_prompt()}\n"
        "Pick exactly ONE action.  Respond with a single JSON object "
        "matching the EscalationAction schema (the schema allows both "
        "creative and ops actions -- the action discriminator picks "
        "the variant).  Include an 'llm_reasoning' field with a 1-2 "
        "sentence rationale."
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def supervisor_escalate_scope(
    scope: EscalationScope,
    *,
    model: Optional[str] = None,
    store: Optional[ArtifactCritiqueStore] = None,
) -> EscalationAction:
    """Consult the pull-based supervisor for a scope.

    The returned :class:`EscalationAction` is validated (same dataclass
    + bounds as the push-based path).  If a primary artifact is in
    scope, an :class:`EscalationRef` is appended to the artifact's
    :class:`ArtifactCritiqueRecord` so future escalations see the chain.
    """
    _counters.incr("escalations")
    system = _SYSTEM_INSTRUCTION
    tools = READ_TOOLS_BY_NAME

    runner = _supervisor_runner
    chosen_model = model or _default_model(scope)

    try:
        if runner is not None:
            action = runner(scope, system, tools, chosen_model)
        else:
            action = _default_runner(scope, system, tools, chosen_model)
    except EscalationActionError:
        raise
    except Exception as exc:
        _counters.incr("fallback_decisions")
        logger.error(
            "supervisor_escalate_scope: runner raised %s; falling back to "
            "abort_run for scope_id=%s",
            exc, scope.scope_id,
        )
        action = EscalationAction(
            action="abort_run",
            reason=(
                f"supervisor_escalate_scope fallback: {exc} "
                f"(scope_id={scope.scope_id})"
            )[:500],
            llm_model=chosen_model,
            llm_reasoning="Pull-based runner raised; deterministic fallback.",
        )

    if not action.llm_model:
        action.llm_model = chosen_model

    logger.info(
        "supervisor_escalate_scope: chose action=%s (L%d) for scope=%s",
        action.action, action.level, scope.scope_id,
    )

    _maybe_record_escalation(scope, action, store=store)
    return action


def _default_model(scope: EscalationScope) -> str:
    flash = os.environ.get(
        "SUPERVISOR_ESCALATE_FLASH_MODEL", "gemini-2.5-flash"
    )
    pro = os.environ.get(
        "SUPERVISOR_ESCALATE_PRO_MODEL", "gemini-2.5-pro"
    )
    return pro if scope.high_cost else flash


# ---------------------------------------------------------------------------
# Recording escalations back into the critique store
# ---------------------------------------------------------------------------

def _maybe_record_escalation(
    scope: EscalationScope,
    action: EscalationAction,
    *,
    store: Optional[ArtifactCritiqueStore],
) -> None:
    """Best-effort: append an EscalationRef to the primary artifact's record."""
    if scope.primary_artifact_id is None or scope.primary_artifact_type is None:
        return
    try:
        effective_store = store or get_critique_store()
    except Exception as exc:
        logger.debug("record_escalation: store resolve failed: %s", exc)
        return

    # Encode the richer decision metadata into ``reasoning`` so the
    # dependency-free ``EscalationRef`` dataclass survives without
    # schema churn.  Downstream consumers can ``json.loads`` the
    # "details=" suffix if they need the structured fields back.
    details_json = json.dumps(
        {
            "failure_kind": scope.failure_kind,
            "stage": scope.stage_name,
            "scope_tags": list(scope.scope_tags),
            "action": action.action,
            "level": action.level,
            "action_params": action.to_dict(),
            "llm_model": action.llm_model or "",
            "decided_by": "supervisor_escalate_scope",
            "decided_at": time.time(),
        },
        default=str,
    )
    reasoning = (action.llm_reasoning or "").strip()
    ref = EscalationRef(
        scope_id=scope.scope_id,
        action=action.action,
        outcome="unknown",
        reasoning=f"{reasoning}\n--- details ---\n{details_json}".strip(),
    )
    try:
        effective_store.append_escalation(
            scope.primary_artifact_type,
            scope.primary_artifact_id,
            ref,
        )
        _counters.incr("recorded_refs")
    except Exception as exc:
        logger.warning(
            "append_escalation failed for %s:%s scope=%s: %s",
            scope.primary_artifact_type, scope.primary_artifact_id,
            scope.scope_id, exc,
        )


# ---------------------------------------------------------------------------
# Default runner: google.genai function-calling loop
# ---------------------------------------------------------------------------

_MAX_TOOL_STEPS = 8         # enough for ~2-3 artifact reads + 1 fleet read.
_MAX_PARSE_RETRIES = 2


def _default_runner(
    scope: EscalationScope,
    system: str,
    tools: dict[str, ReadToolSpec],
    model: str,
) -> EscalationAction:
    """Pure-Python pull-based runner using google.genai function calling.

    Falls back to the legacy push-based supervisor (which uses structured
    JSON output, no tool calls) if:

      * ``google.genai`` is not importable, OR
      * no API key is configured, OR
      * the function-calling loop exceeds ``_MAX_TOOL_STEPS`` without
        producing a final action.

    This keeps the decision layer alive even in degraded environments.
    """
    try:
        from google import genai  # type: ignore
        from google.genai import types as genai_types  # type: ignore
    except Exception:
        logger.info(
            "google.genai unavailable — falling back to push-based supervisor"
        )
        return _push_fallback(scope, model)

    api_key = (
        os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_GENAI_API_KEY")
    )
    if not api_key:
        logger.info(
            "no Gemini API key — falling back to push-based supervisor"
        )
        return _push_fallback(scope, model)

    client = genai.Client(api_key=api_key)

    # Build function-calling tool specs.
    fn_decls = [
        genai_types.FunctionDeclaration(
            name=t.name,
            description=t.description,
            parameters=t.parameters,
        )
        for t in tools.values()
    ]
    tool_obj = genai_types.Tool(function_declarations=fn_decls)

    contents: list[Any] = [
        genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=build_user_prompt(scope))],
        )
    ]

    last_error: Optional[Exception] = None
    parse_retries = 0
    for _step in range(_MAX_TOOL_STEPS):
        _counters.incr("llm_calls")
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system,
                    tools=[tool_obj],
                    temperature=0.2,
                ),
            )
        except Exception as exc:
            last_error = exc
            logger.warning("supervisor LLM call failed: %s", exc)
            break

        # Inspect the candidate for either tool calls or final text.
        parts = _extract_parts(response)
        tool_calls = [p for p in parts if getattr(p, "function_call", None)]
        if tool_calls:
            # Execute each tool call and feed results back.
            contents.append(
                genai_types.Content(role="model", parts=list(parts))
            )
            response_parts: list[Any] = []
            for part in tool_calls:
                fc = part.function_call
                result = _dispatch_tool_call(
                    fc.name, dict(fc.args or {}), tools
                )
                response_parts.append(
                    genai_types.Part.from_function_response(
                        name=fc.name,
                        response={"result": result},
                    )
                )
            contents.append(
                genai_types.Content(role="user", parts=response_parts)
            )
            continue

        # No tool calls — expect a final JSON action in text.
        text = _join_text(parts)
        if text:
            try:
                return _parse_action_text(text)
            except EscalationActionError as exc:
                last_error = exc
                parse_retries += 1
                if parse_retries > _MAX_PARSE_RETRIES:
                    logger.warning(
                        "supervisor pull-loop exceeded %d parse retries — "
                        "push fallback",
                        _MAX_PARSE_RETRIES,
                    )
                    break
                # Retry with an explicit "return only JSON" nudge.
                contents.append(
                    genai_types.Content(role="model", parts=list(parts))
                )
                contents.append(
                    genai_types.Content(
                        role="user",
                        parts=[genai_types.Part(text=(
                            "That was not valid. Return ONLY a single JSON "
                            "object matching the EscalationAction schema — "
                            "no prose, no markdown fences."
                        ))],
                    )
                )
                continue
        # Empty response — break loop.
        break

    _counters.incr("fallback_decisions")
    logger.warning(
        "supervisor pull-loop exhausted %d steps (last_error=%s) — push fallback",
        _MAX_TOOL_STEPS, last_error,
    )
    return _push_fallback(scope, model)


def _dispatch_tool_call(
    name: str,
    args: dict[str, Any],
    tools: dict[str, ReadToolSpec],
) -> Any:
    """Run a single read-tool call; never raises."""
    _counters.incr("tool_calls")
    spec = tools.get(name)
    if spec is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return spec.fn(**args)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:
        logger.warning("tool %s raised: %s", name, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _extract_parts(response: Any) -> list[Any]:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    if content is None:
        return []
    return list(getattr(content, "parts", None) or [])


def _join_text(parts: list[Any]) -> str:
    chunks = []
    for p in parts:
        text = getattr(p, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def _parse_action_text(text: str) -> EscalationAction:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(stripped[start:end + 1])
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
    return EscalationAction.from_dict(data)


def _push_fallback(scope: EscalationScope, model: str) -> EscalationAction:
    """Fall back to the legacy push-based supervisor.

    We synthesise an :class:`EscalationContext` from the scope so the
    existing decision layer still fires at least once — preserving the
    #102 invariant.
    """
    try:
        from agents.production_supervisor import supervisor_escalate
        from orchestrator.escalation_menu import EscalationContext
    except Exception as exc:
        logger.error(
            "push-based supervisor unavailable too (%s); aborting scope %s",
            exc, scope.scope_id,
        )
        return EscalationAction(
            action="abort_run",
            reason=(
                f"pull+push supervisor both unavailable (scope_id="
                f"{scope.scope_id}): {exc}"
            )[:500],
            llm_model=model,
            llm_reasoning="Decision layer unavailable; deterministic fallback.",
        )

    context = EscalationContext(
        failing_artifact=scope.trigger_message,
        artifact_descriptor={
            "scope_id": scope.scope_id,
            "failure_kind": scope.failure_kind,
            "stage_name": scope.stage_name,
            "scope_tags": list(scope.scope_tags),
            "summary_counters": dict(scope.summary_counters),
            "primary_artifact_id": scope.primary_artifact_id or "",
            "primary_artifact_type": scope.primary_artifact_type or "",
        },
        timeline_state_snapshot={},
        escalation_history=list(
            (scope.metadata.get("legacy_context") or {}).get(
                "escalation_history", []
            )
        ),
        high_cost=scope.high_cost,
    )
    return supervisor_escalate(context, model=model)


# ---------------------------------------------------------------------------
# Route-through helper for recovery.py
# ---------------------------------------------------------------------------

def route_context_through_scope(
    context: Any,
    *,
    failure_kind: str = "unknown",
    stage_name: str = "",
    primary_artifact_id: Optional[str] = None,
    primary_artifact_type: Optional[str] = None,
    scope_tags: Optional[list[str]] = None,
    model: Optional[str] = None,
    store: Optional[ArtifactCritiqueStore] = None,
) -> EscalationAction:
    """Bridge: given a legacy EscalationContext, run the pull-based path.

    Callers in ``recovery.py`` / ``recovery_agents.py`` can opt into the
    new pull-based path without rewriting their sites:

    .. code-block:: python

        from agents.escalation_supervisor import route_context_through_scope

        action = route_context_through_scope(
            context,
            failure_kind="qa_fail",
            stage_name="production",
            primary_artifact_id=clip_id,
            primary_artifact_type="clip",
            scope_tags=["jury_split"],
        )
    """
    scope = EscalationScope.from_context(
        context,
        failure_kind=failure_kind,  # type: ignore[arg-type]
        stage_name=stage_name,
        primary_artifact_id=primary_artifact_id,
        primary_artifact_type=primary_artifact_type,  # type: ignore[arg-type]
        scope_tags=list(scope_tags or []),
    )
    return supervisor_escalate_scope(scope, model=model, store=store)


__all__ = [
    "ReadToolSpec",
    "READ_TOOLS",
    "READ_TOOLS_BY_NAME",
    "SupervisorRunner",
    "set_supervisor_runner",
    "get_scope_counters",
    "reset_scope_counters",
    "supervisor_escalate_scope",
    "route_context_through_scope",
    "build_user_prompt",
    "ACTION_NAMES",
    "ESCALATION_ACTION_JSON_SCHEMA",
]
