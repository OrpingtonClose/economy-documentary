"""FastAPI surface for a strands.Agent — HTTP base protocol.

Each agent runs as an independent HTTP service.
All endpoints speak free-flowing plain text (text/plain).

  GET  /  — inspect agent. Never interrupts running work.
  POST /  — interrupt current work, process text as new task, return result.
"""

from __future__ import annotations

import logging
from fastapi import FastAPI, Request
from fastapi.responses import Response
from strands import Agent

logger = logging.getLogger(__name__)


def build_agent_app(agent: Agent, name: str) -> FastAPI:
    """Construct an HTTP service wrapping a strands.Agent.

    Args:
        agent: The strands.Agent instance to expose.
        name: Human-readable agent name (scenario, audio, etc.).

    Returns:
        FastAPI app ready to serve.
    """
    from strands.agent.conversation_manager import SlidingWindowConversationManager

    # Replace default SlidingWindowConversationManager with proactive compression.
    # Proactive mode triggers at 70% of the model's context window, preventing
    # wasted round-trips and output-token starvation from ContextWindowOverflowException.
    agent.conversation_manager = SlidingWindowConversationManager(
        window_size=50,
        proactive_compression={"compression_threshold": 0.7},
    )

    app = FastAPI(title=f"agent-{name}")

    # Simple state tracking for GET /
    _last_task: str = ""
    _last_result: str = ""
    _uptime_start: float = __import__("time").time()

    @app.get("/")
    def _inspect() -> Response:
        """Inspect agent without interrupting. Returns free-flowing text."""
        uptime = __import__("time").time() - _uptime_start
        lines = [f"I am the {name} agent."]
        if _last_task:
            lines.append(f"My last task was: {_last_task[:200]}")
        if _last_result:
            lines.append(f"My last result was: {_last_result[:200]}")
        lines.append(f"I have been running for {round(uptime, 1)} seconds.")
        return Response(
            content="\n".join(lines),
            media_type="text/plain",
        )

    @app.post("/")
    async def _invoke(request: Request) -> Response:
        """Receive raw text, invoke agent, return raw text result."""
        nonlocal _last_task, _last_result
        body = await request.body()
        text = body.decode("utf-8").strip()
        if not text:
            return Response(
                content="error: empty body",
                media_type="text/plain",
                status_code=400,
            )

        _last_task = text
        logger.info("Agent '%s' received task: %s", name, text[:80])

        try:
            result = await agent.invoke_async(text)
            result_text = str(result)
            _last_result = result_text
            logger.info("Agent '%s' completed. Result length: %d chars", name, len(result_text))
            return Response(
                content=result_text,
                media_type="text/plain",
            )
        except Exception as exc:
            logger.exception("Agent '%s' failed: %s", name, exc)
            _last_result = f"error: {exc}"
            return Response(
                content=f"error: {exc}",
                media_type="text/plain",
                status_code=500,
            )

    return app
