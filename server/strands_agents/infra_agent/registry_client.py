"""Playground worker-registry deregister client.

On self-destruct the agent hits
``DELETE /playground/workers/{worker_id}`` so the registry stops
advertising this VM before Vast.ai tears it down. Deregistration is
best-effort: if the playground backend is unreachable we log and
continue to the Vast.ai destroy call rather than stranding the VM.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class RegistryDeregisterError(Exception):
    """Raised when deregistration fails for a non-404 reason."""


@dataclass(frozen=True)
class _HttpResponse:
    status_code: int
    text: str


@dataclass
class PlaygroundRegistryClient:
    """Tiny client that can deregister this worker.

    Attributes:
        base_url: Playground backend URL
            (e.g. ``https://staging.playground.local``).
        http_delete: Injectable DELETE for tests.
    """

    base_url: str
    http_delete: Callable[..., "_HttpResponse"] | None = None

    def deregister(self, worker_id: str) -> None:
        """Call ``DELETE /playground/workers/{worker_id}``.

        404 means the registry never saw this worker (or already purged
        it on a prior heartbeat-stale sweep) — treated as success. Any
        other non-2xx raises :class:`RegistryDeregisterError`.
        """
        url = f"{self.base_url.rstrip('/')}/playground/workers/{worker_id}"
        delete = self.http_delete or _default_http_delete

        logger.info("worker_id=<%s>, url=<%s> | registry deregister requested", worker_id, url)
        try:
            response = delete(url=url, timeout=10.0)
        except Exception as exc:
            raise RegistryDeregisterError(
                f"registry deregister network error: {exc}"
            ) from exc

        if 200 <= response.status_code < 300:
            logger.info(
                "worker_id=<%s>, status=<%d> | registry deregister ok",
                worker_id,
                response.status_code,
            )
            return
        if response.status_code == 404:
            logger.info(
                "worker_id=<%s> | registry deregister 404 (never registered), treated as success",
                worker_id,
            )
            return
        raise RegistryDeregisterError(
            f"registry deregister failed status={response.status_code} body={response.text[:500]!r}"
        )


def _default_http_delete(*, url: str, timeout: float) -> _HttpResponse:
    """Default HTTP DELETE via :mod:`requests`."""
    import requests  # noqa: PLC0415

    response = requests.delete(url, timeout=timeout)
    return _HttpResponse(status_code=response.status_code, text=response.text)
