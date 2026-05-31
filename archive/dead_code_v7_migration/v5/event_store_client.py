"""Event Store Client — async HTTP client for agents and projections.

Every agent, projection, and the watcher loop uses this client to talk to the
Event Store Server (port 8079).  The client is a thin wrapper around httpx that
handles JSON serialisation, effect model conversion, and retry semantics.

Usage:
    client = EventStoreClient("http://localhost:8079")
    await client.start()

    # Append an effect
    seq = await client.append(effect)

    # Read new events for a projection
    events = await client.read_since(run_id, last_sequence)

    # Full replay
    events = await client.replay(run_id)

    await client.close()
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from effects import Effect

logger = logging.getLogger("event_store_client")


class EventStoreClient:
    """Async HTTP client for the Event Store Server.

    No timeouts — architecture policy.  If the server is unreachable,
    the caller waits until the operator intervenes.
    """

    def __init__(self, base_url: str = "http://localhost:8079") -> None:
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=None)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Low-level HTTP helpers
    # ------------------------------------------------------------------

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None
        resp = await self._client.post(f"{self.base_url}/", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def _get(self) -> dict[str, Any]:
        assert self._client is not None
        resp = await self._client.get(f"{self.base_url}/")
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # High-level API
    # ------------------------------------------------------------------

    async def append(self, effect: Effect) -> int:
        """Append an effect to the event store.

        Returns the assigned sequence number.  If the same effect_id is
        retried, the original sequence is returned (idempotent).
        """
        # Build payload manually to avoid uuid_utils.UUID serialization warnings
        payload: dict[str, Any] = {}
        for name, field in effect.model_fields.items():
            val = getattr(effect, name)
            if name == "effect_id":
                payload[name] = str(val)
            else:
                payload[name] = val
        req = {
            "cmd": "append",
            "run_id": payload.pop("run_id"),
            "effect_id": payload.pop("effect_id"),
            "kind": payload.pop("kind"),
            "agent": payload.pop("agent"),
            "payload": payload,
            "causation_id": payload.pop("causation_id", ""),
            "correlation_id": payload.pop("correlation_id", ""),
            "trace_id": payload.pop("trace_id", ""),
            "producer": payload.pop("producer", ""),
            "timestamp": payload.pop("timestamp"),
        }
        result = await self._post(req)
        seq = result["sequence"]
        inserted = result["inserted"]
        if not inserted:
            logger.debug("Duplicate effect_id %s — returned original seq %s",
                         effect.effect_id, seq)
        return seq

    async def read_since(self, run_id: str, sequence: int) -> list[dict[str, Any]]:
        """Return all events for run_id with sequence > given value."""
        result = await self._post({
            "cmd": "read_since",
            "run_id": run_id,
            "sequence": sequence,
        })
        return result.get("events", [])

    async def replay(self, run_id: str) -> list[dict[str, Any]]:
        """Return all events for a run, ordered by sequence."""
        result = await self._post({
            "cmd": "replay",
            "run_id": run_id,
        })
        return result.get("events", [])

    async def read_last_n(self, run_id: str, n: int = 20) -> list[dict[str, Any]]:
        """Return the last N events for a run."""
        result = await self._post({
            "cmd": "read_last_n",
            "run_id": run_id,
            "n": n,
        })
        return result.get("events", [])

    async def health(self) -> dict[str, Any]:
        """Return health status and stats from the event store server."""
        return await self._get()
