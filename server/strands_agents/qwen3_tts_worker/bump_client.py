"""Infra-agent bump client.

The worker calls ``POST http://localhost:29230/infra/bump`` on every
request so active traffic resets the guardian's idle timer. The
guardian lives in the same VM and trusts ``localhost`` callers.

Failures on the bump are logged and swallowed — a single missed bump
must never fail a user-facing synthesis request. If the infra agent
process is genuinely down, the lifetime ceiling will still reap the VM.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


DEFAULT_BUMP_URL = "http://127.0.0.1:29230/infra/bump"
DEFAULT_BUMP_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class _BumpResponse:
    status_code: int


@dataclass
class InfraAgentBumpClient:
    """Injectable bump helper.

    Attributes:
        url: The agent's bump endpoint. Defaults to localhost.
        http_post: Injectable POST for tests. When ``None`` the default
            :mod:`requests`-backed implementation is used.
        timeout_s: Per-call timeout. Deliberately short — a missed
            bump must never stall a render.
    """

    url: str = DEFAULT_BUMP_URL
    http_post: Callable[..., "_BumpResponse"] | None = None
    timeout_s: float = DEFAULT_BUMP_TIMEOUT_S

    def bump(self) -> None:
        """Send one best-effort bump. Never raises."""
        post = self.http_post or _default_http_post
        try:
            response = post(url=self.url, timeout=self.timeout_s)
        except Exception as exc:
            logger.warning(
                "url=<%s>, error=<%s> | infra agent bump network error, swallowed",
                self.url,
                exc,
            )
            return

        if 200 <= response.status_code < 300:
            logger.debug("url=<%s> | infra agent bump ok", self.url)
            return
        logger.warning(
            "url=<%s>, status=<%d> | infra agent bump non-2xx, swallowed",
            self.url,
            response.status_code,
        )


def bump_infra_agent(
    *,
    url: str = DEFAULT_BUMP_URL,
    http_post: Callable[..., "_BumpResponse"] | None = None,
    timeout_s: float = DEFAULT_BUMP_TIMEOUT_S,
) -> None:
    """Functional shortcut for callers that don't want a client object."""
    InfraAgentBumpClient(url=url, http_post=http_post, timeout_s=timeout_s).bump()


def _default_http_post(*, url: str, timeout: float) -> _BumpResponse:
    """Default POST via :mod:`requests`."""
    import requests  # noqa: PLC0415

    response = requests.post(url, timeout=timeout)
    return _BumpResponse(status_code=response.status_code)
