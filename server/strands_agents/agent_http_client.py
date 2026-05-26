"""HTTP client for pipeline agents.

Looks like a simple async caller to the pipeline. Talks HTTP underneath.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class AgentHTTPClient:
    """HTTP client for a remote pipeline agent service.

    Wraps a remote agent running as a FastAPI service.  POSTs plain
    text and receives plain text prose back.
    """

    def __init__(self, base_url: str, name: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.name = name

    async def invoke(self, prompt: str) -> str:
        """Invoke the remote agent via HTTP POST and return the prose response."""
        async with httpx.AsyncClient(timeout=None) as client:
            resp = await client.post(
                f"{self.base_url}/",
                content=prompt.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Agent '{self.name}' at {self.base_url} returned {resp.status_code}: {resp.text}"
            )
        return resp.text

    def __repr__(self) -> str:
        return f"AgentHTTPClient(name={self.name}, url={self.base_url})"
