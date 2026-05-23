"""Tool extraction layer — turn raw tool JSON into typed Pydantic models.

Every tool in the pipeline returns a JSON string. This layer:

    1. Identifies which tool was called
    2. Extracts the appropriate Pydantic model from the raw JSON text
    3. Records the typed outcome to the snapshot store
    4. Returns the typed object (or the raw text — agent still reasons over text)

The agent STILL sees raw text. The SYSTEM extracts types from that text.
This is the "types implied in text" boundary at the tool layer.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from models.tool_result import (
    NarrationResult,
    OTIOClipResult,
    ToolCallOutcome,
    VideoRenderResult,
)
from structured_extract import extract

logger = logging.getLogger(__name__)

# Map tool name prefixes to their result model
_RESULT_MODELS: dict[str, type] = {
    "generate_scene_narration": NarrationResult,
    "generate_narration": NarrationResult,
    "submit_gpu_production_job": VideoRenderResult,
    "generate_video_clip": VideoRenderResult,
    "add_narration_to_timeline": OTIOClipResult,
    "add_video_clip_to_timeline": OTIOClipResult,
    "add_narration_clip": OTIOClipResult,
    "add_video_clip": OTIOClipResult,
}


def extract_tool_result(tool_name: str, raw_json: str) -> ToolCallOutcome:
    """Extract a typed result from raw tool JSON output.

    This is called AFTER the tool executes but BEFORE the agent
    (or callback) acts on the result. The agent still receives raw
    text in its conversation. This extraction is for:

    - Snapshot store (typed event logging)
    - Callback decisions (before_tool, after_tool)
    - Recovery logic (detect failures without regex)
    - Grounding ("the tool said 'error' — is it a real error?")
    """
    result_type = "other"
    extracted: Any = None

    # Determine result type from tool name
    for prefix, model_cls in _RESULT_MODELS.items():
        if prefix in tool_name:
            result_type = prefix.split("_")[0]  # narration, video, otio_clip
            try:
                extracted = extract(
                    model_cls,
                    raw_json,
                    system_prompt=(
                        f"Extract a {model_cls.__name__} from the tool's JSON output. "
                        "If the JSON has an 'error' key, the status should reflect failure. "
                        "If the output is clearly a success JSON, status should be positive."
                    ),
                    temperature=0.0,
                )
            except Exception as exc:
                logger.warning("Extraction failed for %s: %s", tool_name, exc)
            break

    # Build generic outcome
    success = False
    error_msg = ""
    suggested = "continue"

    if extracted:
        success = extracted.status in ("generated", "cached", "rendered", "added", "ok", "success")
        error_msg = getattr(extracted, "error", "")
        if error_msg:
            success = False
            suggested = "retry"
        if getattr(extracted, "status", "") == "queued":
            suggested = "continue"  # Wait for worker
    else:
        # Fallback: parse JSON ourselves for quick failure detection
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                if parsed.get("error") or parsed.get("status") == "error":
                    success = False
                    error_msg = str(parsed.get("error", parsed.get("message", "unknown error")))
                    suggested = "retry"
                elif parsed.get("status") in ("ok", "success", "generated", "rendered", "added"):
                    success = True
                    suggested = "continue"
        except (json.JSONDecodeError, ValueError):
            success = bool(raw_json and len(raw_json) > 5)

    return ToolCallOutcome(
        tool_name=tool_name,
        success=success,
        result_type=result_type,
        extracted_result=extracted,
        raw_output_preview=raw_json[:200],
        error_message=error_msg,
        suggested_action=suggested,
    )


def wrap_tool(tool_func: Callable) -> Callable:
    """Decorator that adds extraction to any tool function.

    Usage:
        @wrap_tool
        def my_tool(arg: str) -> str:
            return json.dumps({"status": "ok"})

    The tool still returns raw JSON (for the agent). The extraction
    happens as a side effect and is recorded.
    """

    def wrapper(*args: Any, **kwargs: Any) -> str:
        raw_result = tool_func(*args, **kwargs)
        tool_name = getattr(tool_func, "__name__", "unknown")

        outcome = extract_tool_result(tool_name, str(raw_result))

        # Record to snapshot store if available
        try:
            from tracing.snapshot_store import get_store
            store = get_store()
            store.record_tool_call(
                agent="",
                tool_name=tool_name,
                args={"args": str(args), "kwargs": str(kwargs)},
                result=outcome.model_dump(mode="json"),
                duration_ms=0.0,
                run_id="",
            )
        except Exception:
            pass  # Snapshot store is best-effort

        logger.debug(
            "Tool %s: success=%s suggested=%s",
            tool_name, outcome.success, outcome.suggested_action,
        )

        return str(raw_result)

    return wrapper
