"""Base agent for persistent state across HTTP calls.

Each agent runs in its own process and maintains conversation history.
Agents output free text. The instructor parses it into effects.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Body, FastAPI
from fastapi.responses import PlainTextResponse
from pydantic_deep import create_deep_agent


class BaseAgent:
    """Base class for persistent pydantic-deep agents.

    Each agent process holds one instance. Message history persists
    across HTTP calls so the agent remembers its previous reasoning.
    Agents output free text. The instructor parses it.
    """

    def __init__(
        self,
        model: str,
        instructions: str,
        include_memory: bool = True,
        include_subagents: bool = False,
        web_search: bool = False,
        web_fetch: bool = False,
        thinking: bool = False,
    ) -> None:
        self._agent = create_deep_agent(
            model=model,
            instructions=instructions,
            include_memory=include_memory,
            include_subagents=include_subagents,
            web_search=web_search,
            web_fetch=web_fetch,
            thinking=thinking,
        )
        self._message_history: list[Any] = []

    def register_tool(self, fn: Callable) -> Callable:
        """Register a tool for agents that need bash execution."""
        self._agent.tool_plain(fn)
        return fn

    async def run(self, user_prompt: str) -> str:
        """Run the agent with persistent message history."""
        result = await self._agent.run(
            user_prompt,
            message_history=self._message_history,
            deps=self._agent.deps_type(),
        )
        self._message_history = result.all_messages()
        return str(result.output)

    def reset(self) -> None:
        """Clear message history."""
        self._message_history = []


def make_fastapi_app(agent: BaseAgent) -> FastAPI:
    """Create a FastAPI app that routes POST / to the agent."""
    app = FastAPI()

    @app.post("/")
    async def invoke(text: str = Body(..., media_type="text/plain")):
        output = await agent.run(text)
        return PlainTextResponse(output)

    @app.post("/reset")
    async def reset():
        agent.reset()
        return PlainTextResponse("Agent memory reset.")

    return app
