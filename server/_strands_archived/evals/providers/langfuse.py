"""Langfuse trace provider wrapper.

:class:`strands_evals.providers.langfuse_provider.LangfuseProvider`
imports ``langfuse`` at module import time, which blows up CI unless
the optional ``langfuse`` extra is pinned. We delay the import to the
factory call and surface a typed error so the orchestrator can fall
back to deterministic traces instead of crashing.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_LANGFUSE_ENV_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")


class LangfuseUnavailableError(RuntimeError):
    """Raised when the Langfuse provider cannot be instantiated.

    Either the ``langfuse`` extra is not installed or the required
    environment variables are missing. Callers in CI should catch this
    and fall back to local trace sources rather than aborting.
    """


def is_langfuse_available() -> bool:
    """Return True iff both the ``langfuse`` package and its env vars are present."""
    missing = [k for k in _LANGFUSE_ENV_KEYS[:2] if not os.environ.get(k)]
    if missing:
        return False
    try:
        import langfuse  # noqa: F401 — import check only
    except ImportError:
        return False
    return True


def get_langfuse_provider():  # noqa: ANN201 — LangfuseProvider is import-deferred
    """Return a configured :class:`LangfuseProvider` instance.

    Raises:
        LangfuseUnavailableError: When the ``langfuse`` package is not
            installed or when ``LANGFUSE_PUBLIC_KEY`` /
            ``LANGFUSE_SECRET_KEY`` are missing. The host env var
            defaults to Langfuse Cloud when unset, matching upstream
            behaviour.
    """
    missing = [k for k in _LANGFUSE_ENV_KEYS[:2] if not os.environ.get(k)]
    if missing:
        raise LangfuseUnavailableError(
            f"missing_env_vars=<{missing}> | set Langfuse credentials or skip"
        )
    try:
        from strands_evals.providers.langfuse_provider import LangfuseProvider  # type: ignore[import-not-found]
    except ImportError as exc:
        raise LangfuseUnavailableError(
            f"import_error=<{exc}> | install the `langfuse` extra"
        ) from exc

    logger.debug(
        "host=<%s> | constructing LangfuseProvider",
        os.environ.get("LANGFUSE_HOST", "cloud.langfuse.com"),
    )
    return LangfuseProvider()
