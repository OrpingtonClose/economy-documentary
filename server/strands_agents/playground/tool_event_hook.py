"""Strands hook that pipes tool invocations into the playground event stream.

A long-running agent like :func:`strands_agents.scenario_agent.build_scenario_agent`
spends most of its wall-clock inside its own tool loop —
``generate_scenario`` → ``evaluate_scenario`` × N → ``refine_scenario`` × M
→ ``create_timeline``. Those tool calls are invisible to the playground
event bus: the bus only sees the outer ``task.start`` / ``task.done``
bracket, so the live narrator has nothing new to talk about for 30-200
seconds.

This hook subscribes to Strands' ``BeforeToolCallEvent`` +
``AfterToolCallEvent`` and emits one ``tool.called`` / ``tool.returned``
playground event per invocation, with the tool name, latency, and a
small digest of inputs/outputs. That turns the "silent LLM thinking"
black box into a steady drip of real signal that the narrator can
paraphrase into distinct, pertinent status lines.

The hook is deliberately defensive:

* If the stream's loop isn't attached (e.g. unit-test import) the
  emission drops silently — see :meth:`RunStream.emit_sync`.
* Payload digests are bounded to ~200 chars. An LLM doesn't need the
  full 2 kB of a ``refine_scenario`` result; it needs "refine_scenario
  returned 7 scenes, 420s total".
* Exceptions in the hook are swallowed — a telemetry hook must never
  break the run it observes.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from strands_agents.playground.events import RunStream

logger = logging.getLogger(__name__)

_MAX_DIGEST_CHARS: int = 200


class PlaygroundToolEventEmitter(HookProvider):
    """Emit ``tool.called`` / ``tool.returned`` events for every tool invocation.

    Attributes:
        stream: The playground :class:`RunStream` to emit onto. The
            hook uses :meth:`RunStream.emit_sync` because Strands
            fires tool hooks from whatever thread is running the
            agent (the worker thread spawned by ``asyncio.to_thread``
            in the run endpoint).
    """

    def __init__(self, stream: RunStream) -> None:
        self.stream = stream
        #: Maps ``toolUseId`` → wall-clock start time so we can report
        #: a per-call latency on the ``tool.returned`` event. Keyed by
        #: the Strands ``ToolUse.toolUseId`` which is unique per call.
        self._starts: dict[str, float] = {}
        #: Monotonic counter incremented on every ``tool.called`` so
        #: the narrator can reason about "step 3 of the loop" without
        #: diffing seq numbers.
        self._call_seq: int = 0

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._on_before)
        registry.add_callback(AfterToolCallEvent, self._on_after)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_before(self, event: BeforeToolCallEvent) -> None:
        try:
            use = dict(event.tool_use or {})
            name = str(use.get("name") or "unknown")
            use_id = str(use.get("toolUseId") or "")
            self._starts[use_id] = time.perf_counter()
            self._call_seq += 1
            inputs = use.get("input") or {}
            summary = f"tool.called {name} (step {self._call_seq})"
            detail: dict[str, Any] = {
                "tool": name,
                "step": self._call_seq,
                "tool_use_id": use_id,
                "input_digest": _digest(inputs),
                "input_keys": sorted(inputs.keys()) if isinstance(inputs, dict) else [],
            }
            self.stream.emit_sync("tool.called", summary, detail=detail)
        except Exception as exc:  # noqa: BLE001 — hook must not break run
            logger.debug("PlaygroundToolEventEmitter.before failed: %s", exc)

    def _on_after(self, event: AfterToolCallEvent) -> None:
        try:
            use = dict(event.tool_use or {})
            name = str(use.get("name") or "unknown")
            use_id = str(use.get("toolUseId") or "")
            start = self._starts.pop(use_id, None)
            elapsed_ms = (
                int((time.perf_counter() - start) * 1000) if start is not None else -1
            )
            exc = getattr(event, "exception", None)
            result = getattr(event, "result", None)
            if exc is not None:
                summary = f"tool.returned {name} failed in {elapsed_ms}ms"
                detail: dict[str, Any] = {
                    "tool": name,
                    "elapsed_ms": elapsed_ms,
                    "error_class": type(exc).__name__,
                    "error": str(exc)[:_MAX_DIGEST_CHARS],
                }
            else:
                summary = f"tool.returned {name} in {elapsed_ms}ms"
                detail = {
                    "tool": name,
                    "elapsed_ms": elapsed_ms,
                    "result_digest": _digest(_unwrap_tool_result(result)),
                }
                shape = _result_shape(result)
                if shape:
                    detail["result_shape"] = shape
            self.stream.emit_sync("tool.returned", summary, detail=detail)
        except Exception as exc:  # noqa: BLE001 — hook must not break run
            logger.debug("PlaygroundToolEventEmitter.after failed: %s", exc)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _digest(payload: Any) -> str:
    """Return a short, LLM-friendly one-line digest of ``payload``.

    Truncated to ``_MAX_DIGEST_CHARS`` so the narrator prompt stays
    small even when a tool returns a multi-kB blob.
    """
    try:
        if isinstance(payload, str):
            text = payload
        else:
            text = json.dumps(payload, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        text = repr(payload)
    text = " ".join(text.split())
    if len(text) > _MAX_DIGEST_CHARS:
        return text[: _MAX_DIGEST_CHARS - 1] + "…"
    return text


def _unwrap_tool_result(result: Any) -> Any:
    """Return the JSON-ish payload inside a Strands ``ToolResult``.

    Strands wraps ``@tool`` returns into ``{"content": [{"text": ...}]}``
    or ``{"content": [{"json": ...}]}``. Unwrap one layer so the
    digest reads the tool's actual return value, not the Strands
    envelope.
    """
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return result
    first = content[0]
    if not isinstance(first, dict):
        return result
    if "json" in first:
        return first["json"]
    text = first.get("text")
    if isinstance(text, str):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return result


def _result_shape(result: Any) -> dict[str, Any]:
    """Extract small, narrator-useful structural facts from a tool result."""
    payload = _unwrap_tool_result(result)
    if not isinstance(payload, dict):
        return {}
    shape: dict[str, Any] = {}
    scenes = payload.get("scenes")
    if isinstance(scenes, list):
        shape["num_scenes"] = len(scenes)
        total = 0.0
        for s in scenes:
            if not isinstance(s, dict):
                continue
            dur = s.get("duration_sec") or s.get("duration") or 0.0
            try:
                total += float(dur)
            except (TypeError, ValueError):
                continue
            if total:
                shape["total_duration_sec"] = round(total, 1)
    rating = payload.get("rating")
    if isinstance(rating, str):
        shape["rating"] = rating
    issues = payload.get("issues")
    if isinstance(issues, list):
        shape["num_issues"] = len(issues)
    return shape


__all__ = ["PlaygroundToolEventEmitter"]
