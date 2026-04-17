"""
ReasoningTracePlugin — single-source observability for the documentary pipeline.

Replaces the scattered ADK_DEBUG / PHOENIX_ENABLED / LoggingPlugin trio with
one plugin that:

1. **Stores full LLM request/response content** in the SQLite span DB
   (always on — no env var gates).
2. **Surfaces reasoning chatter to the frontend** via ``emit_agui_event()``
   so the human watching the dashboard sees what each agent is thinking
   in real time.
3. **Logs agent lifecycle** to the Python logger (replaces LoggingPlugin's
   console prints with proper structured logging).

Usage::

    from plugins.reasoning_trace import ReasoningTracePlugin
    plugins = [ReasoningTracePlugin(), ...]  # add to ADK App / Runner

The plugin hooks into every BasePlugin callback point:

- ``before_model_callback``  → captures LLM request, emits "reasoning_request"
- ``after_model_callback``   → captures LLM response, emits "reasoning_response"
- ``before_agent_callback``  → emits "agent_started"
- ``after_agent_callback``   → emits "agent_completed"
- ``before_tool_callback``   → emits "tool_started"
- ``after_tool_callback``    → emits "tool_completed"
- ``on_event_callback``      → emits "agent_event" for text content
- ``on_model_error_callback`` → emits "reasoning_error"
- ``on_tool_error_callback``  → emits "tool_error"

All events are stored in a thread-safe SQLite ``reasoning_log`` table
(separate from the OTel spans table) AND pushed to the AG-UI event bus.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Optional

from google.adk.agents import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from typing_extensions import override

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQLite reasoning log — always-on, no env var required
# ---------------------------------------------------------------------------

_FINDINGS_DIR = os.environ.get(
    "FINDINGS_DIR",
    os.path.join(os.path.expanduser("~"), ".documentary-pipeline"),
)
_REASONING_DB = os.path.join(_FINDINGS_DIR, "reasoning_traces.db")

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS reasoning_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL    NOT NULL,
    event_type  TEXT    NOT NULL,
    agent_name  TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL DEFAULT '',
    content     TEXT    NOT NULL DEFAULT '',
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    metadata    TEXT    NOT NULL DEFAULT '{}'
);
"""

_INSERT = """\
INSERT INTO reasoning_log
    (timestamp, event_type, agent_name, model, content, tokens_in, tokens_out, metadata)
VALUES (?, ?, ?, ?, ?, ?, ?, ?);
"""


class _ReasoningStore:
    """Thread-safe SQLite store for reasoning traces."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        os.makedirs(_FINDINGS_DIR, exist_ok=True)
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                _REASONING_DB, timeout=10, check_same_thread=False
            )
        return self._conn

    def _ensure_schema(self) -> None:
        with self._lock:
            self._get_conn().execute(_CREATE_TABLE)
            self._get_conn().commit()

    def write(
        self,
        event_type: str,
        agent_name: str = "",
        model: str = "",
        content: str = "",
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        row = (
            time.time(),
            event_type,
            agent_name,
            model,
            content,
            tokens_in,
            tokens_out,
            json.dumps(metadata or {}, default=str),
        )
        with self._lock:
            self._get_conn().execute(_INSERT, row)
            self._get_conn().commit()


_store = _ReasoningStore()

# ---------------------------------------------------------------------------
# Content formatting helpers
# ---------------------------------------------------------------------------

_MAX_CONTENT_CHARS = 50_000  # cap per field to prevent runaway storage


def _format_content(content: Optional[types.Content], max_len: int = _MAX_CONTENT_CHARS) -> str:
    """Extract text from a genai Content object."""
    if not content or not content.parts:
        return ""
    parts = []
    for part in content.parts:
        if part.text:
            parts.append(part.text)
        elif part.function_call:
            parts.append(f"[tool_call:{part.function_call.name}]")
        elif part.function_response:
            parts.append(f"[tool_response:{part.function_response.name}]")
    text = "\n".join(parts)
    if len(text) > max_len:
        text = text[:max_len] + f"... (truncated, {len(text)} chars total)"
    return text


def _format_messages(contents: list, max_len: int = _MAX_CONTENT_CHARS) -> str:
    """Format the message history from an LlmRequest."""
    if not contents:
        return ""
    parts = []
    for c in contents:
        role = getattr(c, "role", "?")
        text = _format_content(c) if hasattr(c, "parts") else str(c)
        # Only include the last 2000 chars of each message to keep it reasonable
        if len(text) > 2000:
            text = text[:500] + f"\n... ({len(text)} chars) ...\n" + text[-500:]
        parts.append(f"[{role}] {text}")
    joined = "\n---\n".join(parts)
    if len(joined) > max_len:
        joined = joined[:max_len] + "... (truncated)"
    return joined


def _emit(event_type: str, data: dict) -> None:
    """Push a reasoning event to the AG-UI frontend bus."""
    try:
        from agui import emit_agui_event
        emit_agui_event(event_type, data)
    except Exception:
        pass  # frontend bus not available (e.g. during tests)


# ---------------------------------------------------------------------------
# The plugin
# ---------------------------------------------------------------------------

class ReasoningTracePlugin(BasePlugin):
    """Single-source observability: stores full traces + surfaces to frontend.

    Always on. No env vars needed. Replaces LoggingPlugin + DebugLoggingPlugin
    + scattered PHOENIX_ENABLED / ADK_DEBUG configuration.
    """

    def __init__(self, name: str = "reasoning_trace"):
        super().__init__(name)
        logger.info(
            "ReasoningTracePlugin initialized — traces -> %s", _REASONING_DB
        )

    # -- Agent lifecycle ----------------------------------------------------

    @override
    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Optional[types.Content]:
        agent_name = callback_context.agent_name
        logger.info("[trace] Agent starting: %s", agent_name)
        _store.write("agent_started", agent_name=agent_name)
        _emit("agent_started", {
            "agent": agent_name,
            "timestamp": time.time(),
        })
        return None

    @override
    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Optional[types.Content]:
        agent_name = callback_context.agent_name
        logger.info("[trace] Agent completed: %s", agent_name)
        _store.write("agent_completed", agent_name=agent_name)
        _emit("agent_completed", {
            "agent": agent_name,
            "timestamp": time.time(),
        })
        return None

    # -- LLM lifecycle (the reasoning traces) --------------------------------

    @override
    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        agent_name = callback_context.agent_name
        model = str(llm_request.model or "default")

        # Extract system instruction
        sys_instr = ""
        if llm_request.config and llm_request.config.system_instruction:
            sys_instr = str(llm_request.config.system_instruction)

        # Extract message history (the actual reasoning context)
        messages_text = _format_messages(llm_request.contents or [])

        # Extract available tools
        tools = list(llm_request.tools_dict.keys()) if llm_request.tools_dict else []

        content = messages_text
        metadata = {
            "system_instruction_preview": sys_instr[:500] if sys_instr else "",
            "tools": tools,
            "num_messages": len(llm_request.contents) if llm_request.contents else 0,
        }

        _store.write(
            "llm_request",
            agent_name=agent_name,
            model=model,
            content=content,
            metadata=metadata,
        )

        # Emit a concise version to the frontend (full content is in the DB)
        last_msg = ""
        if llm_request.contents:
            last_content = llm_request.contents[-1]
            last_msg = _format_content(last_content, max_len=300)

        _emit("reasoning_request", {
            "agent": agent_name,
            "model": model,
            "tools": tools,
            "last_message_preview": last_msg,
            "num_messages": metadata["num_messages"],
            "timestamp": time.time(),
        })

        logger.info(
            "[trace] LLM request: agent=%s model=%s messages=%d tools=%s",
            agent_name, model, metadata["num_messages"], tools,
        )
        return None

    @override
    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> Optional[LlmResponse]:
        agent_name = callback_context.agent_name

        # Extract response content
        response_text = _format_content(llm_response.content)

        # Extract token counts
        tokens_in = None
        tokens_out = None
        if llm_response.usage_metadata:
            tokens_in = llm_response.usage_metadata.prompt_token_count
            tokens_out = llm_response.usage_metadata.candidates_token_count

        # Extract error info
        error_info = {}
        if llm_response.error_code:
            error_info = {
                "error_code": str(llm_response.error_code),
                "error_message": str(llm_response.error_message or ""),
            }

        metadata = {
            "partial": llm_response.partial,
            "turn_complete": llm_response.turn_complete,
            **error_info,
        }

        _store.write(
            "llm_response",
            agent_name=agent_name,
            content=response_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            metadata=metadata,
        )

        # Emit to frontend — include the actual response text so the user
        # can see what the agent is thinking in real time
        preview = response_text[:500]
        if len(response_text) > 500:
            preview += f"... ({len(response_text)} chars)"

        _emit("reasoning_response", {
            "agent": agent_name,
            "content": preview,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "error": error_info.get("error_message"),
            "timestamp": time.time(),
        })

        logger.info(
            "[trace] LLM response: agent=%s tokens=%s/%s content=%d chars",
            agent_name, tokens_in, tokens_out, len(response_text),
        )
        return None

    @override
    async def on_model_error_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> Optional[LlmResponse]:
        agent_name = callback_context.agent_name
        model = str(llm_request.model or "default")

        _store.write(
            "llm_error",
            agent_name=agent_name,
            model=model,
            content=str(error),
        )
        _emit("reasoning_error", {
            "agent": agent_name,
            "model": model,
            "error": str(error),
            "timestamp": time.time(),
        })
        logger.error("[trace] LLM error: agent=%s error=%s", agent_name, error)
        return None

    # -- Tool lifecycle -----------------------------------------------------

    @override
    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        agent_name = tool_context.agent_name
        tool_name = tool.name

        args_str = json.dumps(tool_args, default=str)
        if len(args_str) > _MAX_CONTENT_CHARS:
            args_str = args_str[:_MAX_CONTENT_CHARS] + "..."

        _store.write(
            "tool_started",
            agent_name=agent_name,
            content=args_str,
            metadata={"tool": tool_name},
        )
        _emit("tool_started", {
            "agent": agent_name,
            "tool": tool_name,
            "args_preview": args_str[:300],
            "timestamp": time.time(),
        })
        logger.info("[trace] Tool starting: %s.%s", agent_name, tool_name)
        return None

    @override
    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> Optional[dict]:
        agent_name = tool_context.agent_name
        tool_name = tool.name

        result_str = json.dumps(result, default=str)
        if len(result_str) > _MAX_CONTENT_CHARS:
            result_str = result_str[:_MAX_CONTENT_CHARS] + "..."

        _store.write(
            "tool_completed",
            agent_name=agent_name,
            content=result_str,
            metadata={"tool": tool_name},
        )
        _emit("tool_completed", {
            "agent": agent_name,
            "tool": tool_name,
            "result_preview": result_str[:300],
            "timestamp": time.time(),
        })
        logger.info("[trace] Tool completed: %s.%s", agent_name, tool_name)
        return None

    @override
    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> Optional[dict]:
        agent_name = tool_context.agent_name
        tool_name = tool.name

        _store.write(
            "tool_error",
            agent_name=agent_name,
            content=str(error),
            metadata={"tool": tool_name, "args": json.dumps(tool_args, default=str)[:500]},
        )
        _emit("tool_error", {
            "agent": agent_name,
            "tool": tool_name,
            "error": str(error),
            "timestamp": time.time(),
        })
        logger.error(
            "[trace] Tool error: %s.%s -> %s", agent_name, tool_name, error
        )
        return None

    # -- Event lifecycle ----------------------------------------------------

    @override
    async def on_event_callback(
        self, *, invocation_context: InvocationContext, event: Event
    ) -> Optional[Event]:
        author = event.author or ""
        content_text = _format_content(event.content, max_len=1000)

        # Only log events with actual content (skip empty heartbeats)
        if content_text:
            _store.write(
                "agent_event",
                agent_name=author,
                content=content_text,
                metadata={
                    "event_id": event.id,
                    "is_final": event.is_final_response(),
                },
            )
            # Surface text content to the frontend
            if content_text and not content_text.startswith("[tool_"):
                _emit("agent_event", {
                    "agent": author,
                    "content": content_text[:500],
                    "is_final": event.is_final_response(),
                    "timestamp": time.time(),
                })

        return None

    # -- Invocation lifecycle -----------------------------------------------

    @override
    async def before_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> Optional[types.Content]:
        agent_name = (
            invocation_context.agent.name
            if hasattr(invocation_context.agent, "name")
            else "unknown"
        )
        _store.write("invocation_started", agent_name=agent_name)
        _emit("invocation_started", {
            "agent": agent_name,
            "invocation_id": invocation_context.invocation_id,
            "timestamp": time.time(),
        })
        logger.info("[trace] Invocation started: %s", agent_name)
        return None

    @override
    async def after_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        agent_name = (
            invocation_context.agent.name
            if hasattr(invocation_context.agent, "name")
            else "unknown"
        )
        _store.write("invocation_completed", agent_name=agent_name)
        _emit("invocation_completed", {
            "agent": agent_name,
            "invocation_id": invocation_context.invocation_id,
            "timestamp": time.time(),
        })
        logger.info("[trace] Invocation completed: %s", agent_name)
        return None
