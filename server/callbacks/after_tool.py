"""
After-tool callback -- tool result truncation + dashboard tracking.

Ported from MiroThinker. Adapted for documentary pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

from callbacks.before_tool import _provider_semaphores
from dashboard import get_active_collector

logger = logging.getLogger(__name__)

TOOL_RESULT_MAX_CHARS = int(os.environ.get("TOOL_RESULT_MAX_CHARS", "25000"))

# Tools whose results can be very large
_TRUNCATABLE_TOOLS = {
    "get_timeline_status",
    "validate_timeline",
    "align_narration",
    "probe_clip",
}


def _maybe_truncate_result(tool_name: str, result_text: str) -> str:
    """Truncate large tool results to stay within context budget."""
    if not isinstance(result_text, str):
        return result_text

    max_chars = TOOL_RESULT_MAX_CHARS

    if tool_name not in _TRUNCATABLE_TOOLS:
        if len(result_text) > 100_000:
            logger.info(
                "Truncating unexpected large result from %s: %d -> 100000 chars",
                tool_name,
                len(result_text),
            )
            return result_text[:100_000] + "\n[truncated]"
        return result_text

    if len(result_text) <= max_chars:
        return result_text

    # Try JSON-aware truncation
    try:
        parsed = json.loads(result_text)
        text = parsed.get("text", "")
        if text and len(text) > max_chars:
            parsed["text"] = text[:max_chars] + "\n[truncated]"
            return json.dumps(parsed, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    logger.info(
        "Truncating %s result: %d -> %d chars",
        tool_name,
        len(result_text),
        max_chars,
    )
    return result_text[:max_chars] + "\n[truncated]"


def after_tool_callback(
    tool: Any,
    args: Dict[str, Any],
    tool_context: Any,
    tool_response: Any,
) -> Optional[Dict[str, Any]]:
    """ADK after_tool_callback."""
    tool_name: str = tool.name if hasattr(tool, "name") else str(tool)
    result_text = str(tool_response) if tool_response is not None else ""

    # -- Release provider semaphore --------------------------------------------
    call_id = getattr(tool_context, "function_call_id", "") or tool_name
    sem_key = f"_provider_sem_{call_id}"
    held_provider = tool_context.state.get(sem_key, "")
    if held_provider:
        _provider_semaphores[held_provider].release()
        tool_context.state[sem_key] = ""

    # -- Dashboard: track tool end ---------------------------------------------
    start_time = tool_context.state.get(f"_tool_start_time_{call_id}", 0)
    duration = time.time() - start_time if start_time else 0.0
    _c = get_active_collector()
    if _c:
        agent_name = getattr(tool_context, "agent_name", "pipeline")
        _c.tool_end(
            tool_name,
            agent_name,
            duration,
            result_chars=len(result_text),
        )

    # -- Typed extraction from raw tool result --------------------------------
    # "Types implied in text" — the tool returns raw JSON. The agent still
    # reasons over text. But the system extracts structure for observability
    # and recovery decisions.
    try:
        from tool_extract import extract_tool_result
        outcome = extract_tool_result(tool_name, result_text)
        if not outcome.success:
            logger.warning(
                "Tool %s failed: %s (suggested: %s)",
                tool_name,
                outcome.error_message,
                outcome.suggested_action,
            )
        # Record to snapshot store
        try:
            from tracing.snapshot_store import get_store
            store = get_store()
            agent_name = getattr(tool_context, "agent_name", "pipeline")
            store.record_tool_call(
                agent=agent_name,
                tool_name=tool_name,
                args=dict(args),
                result=outcome.model_dump(mode="json"),
                duration_ms=duration * 1000,
                run_id=tool_context.state.get("_run_id", "unknown"),
            )
        except Exception:
            pass  # Snapshot store is best-effort
    except Exception as exc:
        logger.debug("Tool extraction failed for %s: %s", tool_name, exc)

    # -- Tool result truncation ------------------------------------------------
    truncated = _maybe_truncate_result(tool_name, result_text)
    if truncated != result_text:
        return {"result": truncated}

    return None
