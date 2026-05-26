"""Playground worker-registry client for the video role.

The worker self-registers with ``POST /playground/workers`` on boot,
heartbeats periodically, and unregisters on clean shutdown. The video
role does **not** pin a voice, so there is no ``pin_voice`` call on
this client — that is a TTS concern.

All errors raised by this client inherit from :class:`RegistryClientError`
so the runner can gate retry/back-off behaviour on one exception type.
"""

from __future__ import annotations

import json as _json
import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_S = 10.0


class RegistryClientError(Exception):
    """Base class for registry client errors."""


class RegistryRegisterError(RegistryClientError):
    """Raised on registration failure (non-2xx)."""


class RegistryHeartbeatError(RegistryClientError):
    """Raised on heartbeat failure (non-2xx, or 404 when unexpectedly gone)."""


class RegistryUnregisterError(RegistryClientError):
    """Raised on unregister failure (non-2xx for any reason except 404)."""


@dataclass(frozen=True)
class _HttpResponse:
    status_code: int
    text: str


HttpPost = Callable[..., "_HttpResponse"]
HttpDelete = Callable[..., "_HttpResponse"]


@dataclass
class PlaygroundRegistryClient:
    """Worker-side client against ``/playground/workers`` endpoints.

    Attributes:
        base_url: Playground backend base URL.
            E.g. ``https://playground.example``.
        http_post: Injectable POST (for tests). Defaults to
            :mod:`requests`-backed implementation.
        http_delete: Injectable DELETE (for tests). Defaults to
            :mod:`requests`-backed implementation.
        timeout_s: Per-call timeout.
    """

    base_url: str
    http_post: HttpPost | None = None
    http_delete: HttpDelete | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S

    def register(
        self,
        *,
        worker_id: str,
        role: str,
        endpoint_url: str,
        vram_gb: int,
    ) -> dict:
        """Register this VM as a worker. Returns the server payload."""
        url = f"{self.base_url.rstrip('/')}/playground/workers"
        body: dict[str, object] = {
            "worker_id": worker_id,
            "role": role,
            "endpoint_url": endpoint_url,
            "vram_gb": vram_gb,
        }

        logger.info(
            "worker_id=<%s>, role=<%s>, vram_gb=<%d>, url=<%s> | registering",
            worker_id,
            role,
            vram_gb,
            url,
        )
        response = self._post_json(url, body)

        if 200 <= response.status_code < 300:
            return _decode_json(response.text)
        raise RegistryRegisterError(
            f"register failed status={response.status_code} body={response.text[:500]!r}"
        )

    def heartbeat(
        self,
        *,
        worker_id: str,
        free_vram_gb: int | None = None,
    ) -> dict:
        """Send a heartbeat. Returns the server payload."""
        url = (
            f"{self.base_url.rstrip('/')}/playground/workers/"
            f"{worker_id}/heartbeat"
        )
        body: dict[str, object] = {}
        if free_vram_gb is not None:
            body["free_vram_gb"] = free_vram_gb

        response = self._post_json(url, body)
        if 200 <= response.status_code < 300:
            return _decode_json(response.text)
        raise RegistryHeartbeatError(
            f"heartbeat failed status={response.status_code} body={response.text[:500]!r}"
        )

    def unregister(self, *, worker_id: str) -> None:
        """Best-effort deregister. 404 is treated as success."""
        url = (
            f"{self.base_url.rstrip('/')}/playground/workers/{worker_id}"
        )
        delete = self.http_delete or _default_http_delete
        try:
            response = delete(url=url, timeout=self.timeout_s)
        except Exception as exc:
            raise RegistryUnregisterError(
                f"unregister network error: {exc}"
            ) from exc

        if 200 <= response.status_code < 300:
            return
        if response.status_code == 404:
            logger.info(
                "worker_id=<%s> | unregister 404, treated as success",
                worker_id,
            )
            return
        raise RegistryUnregisterError(
            f"unregister failed status={response.status_code} body={response.text[:500]!r}"
        )

    def _post_json(self, url: str, body: dict[str, object]) -> _HttpResponse:
        post = self.http_post or _default_http_post
        try:
            return post(url=url, json=body, timeout=self.timeout_s)
        except Exception as exc:
            raise RegistryClientError(
                f"registry POST network error url={url} error={exc}"
            ) from exc


def _default_http_post(
    *, url: str, json: dict[str, object], timeout: float
) -> _HttpResponse:
    """Default POST via :mod:`requests`."""
    import requests  # noqa: PLC0415

    response = requests.post(url, json=json, timeout=timeout)
    return _HttpResponse(status_code=response.status_code, text=response.text)


def _default_http_delete(*, url: str, timeout: float) -> _HttpResponse:
    """Default DELETE via :mod:`requests`."""
    import requests  # noqa: PLC0415

    response = requests.delete(url, timeout=timeout)
    return _HttpResponse(status_code=response.status_code, text=response.text)


def _decode_json(text: str) -> dict:
    if not text:
        return {}
    try:
        decoded = _json.loads(text)
    except ValueError:
        return {"raw": text}
    if isinstance(decoded, dict):
        return decoded
    return {"value": decoded}
