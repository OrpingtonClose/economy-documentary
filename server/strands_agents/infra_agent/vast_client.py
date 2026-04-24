"""Vast.ai destroy client.

The agent only needs one operation: destroy its own instance by id.
Everything else (provisioning, listing, SSH key management) happens on
the orchestrator side via ``vastai`` CLI. This module is intentionally
tiny and mockable.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

VAST_API_BASE: str = "https://console.vast.ai/api/v0"
VAST_API_KEY_ENV: str = "VAST_AI_API_KEY"


class VastAiDestroyError(Exception):
    """Raised when Vast.ai refuses or errors on instance destruction.

    The destroy sequence treats ``404 not found`` as success (the
    instance was already gone) but any other non-2xx status or network
    error is a real failure — the agent logs and exits anyway so the
    VM still shuts down when the host drops the process.
    """


@dataclass
class VastAiClient:
    """Minimal Vast.ai REST client for self-destruction.

    Attributes:
        api_key: Vast.ai API token. Defaults to ``$VAST_AI_API_KEY``.
        base_url: Vast.ai REST base. Defaults to :data:`VAST_API_BASE`.
        http_delete: Injectable HTTP DELETE callable for tests. Must
            accept ``(url, headers, timeout)`` and return an object with
            ``status_code: int`` and ``text: str``. Defaults to
            :func:`_default_http_delete` which wraps ``requests.delete``.
    """

    api_key: str | None = None
    base_url: str = VAST_API_BASE
    http_delete: Callable[..., "_HttpResponse"] | None = None

    def destroy_instance(self, instance_id: str | int) -> None:
        """Destroy the given instance. Idempotent against 404.

        Args:
            instance_id: Vast.ai instance id.

        Raises:
            VastAiDestroyError: On any non-2xx response other than 404,
                or on missing API key, or on network error.
        """
        key = self.api_key or os.environ.get(VAST_API_KEY_ENV)
        if not key:
            raise VastAiDestroyError(
                f"vast.ai api key not set; export {VAST_API_KEY_ENV}"
            )

        url = f"{self.base_url.rstrip('/')}/instances/{instance_id}/"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
        }
        delete = self.http_delete or _default_http_delete

        logger.info(
            "instance_id=<%s>, url=<%s> | vast.ai destroy requested",
            instance_id,
            url,
        )
        try:
            response = delete(url=url, headers=headers, timeout=15.0)
        except Exception as exc:
            raise VastAiDestroyError(
                f"vast.ai destroy network error: {exc}"
            ) from exc

        if 200 <= response.status_code < 300:
            logger.info(
                "instance_id=<%s>, status=<%d> | vast.ai destroy ok",
                instance_id,
                response.status_code,
            )
            return
        if response.status_code == 404:
            logger.info(
                "instance_id=<%s> | vast.ai destroy 404 (already gone), treated as success",
                instance_id,
            )
            return
        raise VastAiDestroyError(
            f"vast.ai destroy failed status={response.status_code} body={response.text[:500]!r}"
        )


@dataclass(frozen=True)
class _HttpResponse:
    """Minimal protocol-like dataclass to mirror ``requests.Response``.

    Kept internal; tests supply duck-typed stubs. The real
    :func:`_default_http_delete` returns the underlying
    ``requests.Response`` which has the same two attributes.
    """

    status_code: int
    text: str


def _default_http_delete(
    *,
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> _HttpResponse:
    """Default HTTP DELETE via :mod:`requests`.

    Imported lazily so ``requests`` is only required on the VM, not in
    the unit test environment (where ``http_delete`` is always injected).
    """
    import requests  # noqa: PLC0415

    response = requests.delete(url, headers=headers, timeout=timeout)
    return _HttpResponse(status_code=response.status_code, text=response.text)
