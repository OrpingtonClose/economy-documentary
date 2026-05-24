"""HTTP client for strands.Agent — satisfies AgentBase protocol.

Looks like a strands.Agent to the Graph. Talks HTTP underneath.
Converts ContentBlock lists to plain text for POST, wraps plain text
responses back into AgentResult objects the Graph can consume.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

import httpx

from strands.agent.agent_result import AgentResult
from strands.telemetry.metrics import EventLoopMetrics
from strands.types.content import ContentBlock
from strands.types.event_loop import Metrics, Usage

logger = logging.getLogger(__name__)


def _content_blocks_to_text(prompt: Any) -> str:
    """Flatten a prompt (str, ContentBlock list, etc.) into plain text."""
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        parts = []
        for block in prompt:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
            elif hasattr(block, "get") and callable(block.get):
                txt = block.get("text")
                if txt:
                    parts.append(txt)
        return "\n".join(parts)
    return str(prompt)


def _text_to_agent_result(text: str) -> AgentResult:
    """Wrap a plain text HTTP response into an AgentResult the Graph expects."""
    message: dict[str, Any] = {
        "content": [ContentBlock(text=text)],
        "role": "assistant",
    }
    metrics = EventLoopMetrics(
        accumulated_usage=Usage(inputTokens=0, outputTokens=0, totalTokens=0),
        accumulated_metrics=Metrics(latencyMs=0),
    )
    return AgentResult(
        stop_reason="end_turn",
        message=message,  # type: ignore[arg-type]
        metrics=metrics,
        state={},
    )


class AgentHTTPClient:
    """HTTP client proxy for a remote strands.Agent service.

    Satisfies the AgentBase protocol (invoke_async, stream_async, __call__)
    so the Graph can use it as a GraphNode.executor without knowing the
    difference between local and remote agents.
    """

    def __init__(self, base_url: str, name: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.name = name

    async def invoke_async(
        self,
        prompt: Any = None,
        **kwargs: Any,
    ) -> AgentResult:
        """Invoke the remote agent via HTTP POST and return an AgentResult."""
        import asyncio
        await asyncio.sleep(2.0)  # throttle: prevent tight loops between agents
        text = _content_blocks_to_text(prompt)
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(f"{self.base_url}/", content=text)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Agent '{self.name}' at {self.base_url} returned {resp.status_code}: {resp.text}"
            )
        return _text_to_agent_result(resp.text)

    async def stream_async(
        self,
        prompt: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream agent execution.

        Since the remote agent returns a single plain-text response, this
        yields one event with the result — matching the MultiAgentBase
        default implementation pattern.
        """
        result = await self.invoke_async(prompt, **kwargs)
        yield {"result": result}

    def __call__(
        self,
        prompt: Any = None,
        **kwargs: Any,
    ) -> AgentResult:
        """Synchronous wrapper — runs invoke_async in a fresh event loop."""
        import asyncio
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.invoke_async(prompt, **kwargs))
        # If already in an async context, this should not be called
        raise RuntimeError(
            "AgentHTTPClient.__call__ cannot be used from an async context. "
            "Use invoke_async instead."
        )
