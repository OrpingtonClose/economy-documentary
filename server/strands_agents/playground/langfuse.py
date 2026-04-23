"""Langfuse OTel exporter wiring for the Component Playground.

Slice 2 of the Wave 2/3 pipeline (see
``docs/strands-migration/plans/next-wave.md`` §slices). Ships three
things:

1. :func:`is_langfuse_enabled` — cheap env-var check callers use to
   decide whether to surface the "View Trace" affordance.
2. :func:`setup_langfuse_exporter` — install an OTLP HTTP exporter on
   the global ``TracerProvider`` at process start. Idempotent; safe
   to call from tests.
3. :func:`langfuse_trace_url` — deterministic mapping from an OTel
   ``trace_id`` (hex, 32 chars) to the Langfuse trace URL the
   frontend button opens.

**Protocol.** Langfuse accepts vanilla OTLP spans at
``<LANGFUSE_HOST>/api/public/otel/v1/traces`` with HTTP Basic auth
where ``username`` is the public key and ``password`` is the secret
key. No special SDK — we stay on the same ``opentelemetry-exporter-
otlp-proto-http`` package ``server/plugins/__init__.py`` already
pulls in for Phoenix.

**Graceful degradation.** Missing creds / missing host / missing
exporter package are all no-ops that log at ``INFO`` and return
``False``. The playground never refuses to start because observability
is off.

**Trace-id surface.** AG-UI envelopes stay untouched (slice 1); the
trace id lives on the run state payload as ``trace_id`` / ``trace_url``
so the frontend can render a button without touching the event
schema. Same OTel span tree that the AG-UI emitter reads from — see
``docs/strands-migration/plans/next-wave.md`` §observability for the
shared-source rationale.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: The three env vars that gate Langfuse wiring. Public/secret are
#: required; host defaults to Langfuse Cloud when unset to match the
#: upstream ``langfuse`` package behaviour.
_LANGFUSE_PUBLIC_KEY_VAR = "LANGFUSE_PUBLIC_KEY"
_LANGFUSE_SECRET_KEY_VAR = "LANGFUSE_SECRET_KEY"
_LANGFUSE_HOST_VAR = "LANGFUSE_HOST"

#: Fall-through host used when ``LANGFUSE_HOST`` is unset. Matches the
#: upstream SDK default and Langfuse Cloud's production URL.
_DEFAULT_HOST = "https://cloud.langfuse.com"

#: Path the Langfuse server listens on for OTel OTLP HTTP traces. Spelled
#: out here because the Langfuse docs bury it two pages deep and every
#: time we've had to touch it we've had to re-find it.
_OTLP_TRACES_PATH = "/api/public/otel/v1/traces"

#: Module-level guard so repeated ``setup_langfuse_exporter()`` calls
#: (CI / tests / hot reload) don't stack span processors on the global
#: tracer provider.
_EXPORTER_INSTALLED: bool = False


def _public_key() -> str:
    return os.environ.get(_LANGFUSE_PUBLIC_KEY_VAR, "").strip()


def _secret_key() -> str:
    return os.environ.get(_LANGFUSE_SECRET_KEY_VAR, "").strip()


def _host() -> str:
    raw = os.environ.get(_LANGFUSE_HOST_VAR, "").strip()
    return raw or _DEFAULT_HOST


def is_langfuse_enabled() -> bool:
    """Return ``True`` iff both keys are set.

    Host is intentionally not part of the gate — an unset host means
    "use Langfuse Cloud", which is still a valid configuration.
    Callers that care about the concrete URL should pair this with
    :func:`langfuse_host`.
    """
    return bool(_public_key() and _secret_key())


def langfuse_host() -> str:
    """Return the Langfuse host (``LANGFUSE_HOST`` or the cloud default).

    Trailing slashes are stripped so callers can ``f"{host}/path"``
    without worrying about double-slash URLs.
    """
    return _host().rstrip("/")


def langfuse_trace_url(trace_id: str) -> str | None:
    """Return the Langfuse trace-page URL for an OTel ``trace_id``.

    ``trace_id`` must be the 32-char lowercase hex form produced by
    :meth:`opentelemetry.trace.Span.get_span_context`. Returns
    ``None`` when Langfuse is not configured or when the trace id is
    malformed — the frontend uses a ``None`` return to decide not to
    render the "View Trace" button.
    """
    if not is_langfuse_enabled():
        return None
    if not _is_valid_otel_trace_id(trace_id):
        return None
    return f"{langfuse_host()}/trace/{trace_id}"


def _is_valid_otel_trace_id(trace_id: str) -> bool:
    if not isinstance(trace_id, str) or len(trace_id) != 32:
        return False
    try:
        int(trace_id, 16)
    except ValueError:
        return False
    return True


def setup_langfuse_exporter() -> bool:
    """Install an OTLP span processor that exports to Langfuse.

    Idempotent: the first call wires the processor onto the global
    ``TracerProvider``; subsequent calls are no-ops. Returns ``True``
    when the exporter is newly installed (or was already installed in
    this process), ``False`` when it was skipped.

    The exporter is skipped — with a single INFO log — when any of:

    * ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` is unset;
    * ``opentelemetry-exporter-otlp-proto-http`` is not importable;
    * the global tracer provider is the default no-op one (OTel not
      configured at all).

    Ships Basic-auth credentials as an OTLP exporter header so no
    Langfuse-specific SDK is needed — the same exporter type
    ``server/plugins/__init__.py`` uses for Phoenix.
    """
    global _EXPORTER_INSTALLED
    if _EXPORTER_INSTALLED:
        return True

    if not is_langfuse_enabled():
        logger.info(
            "host=<%s>, public_key_present=<%s>, secret_key_present=<%s> "
            "| Langfuse exporter skipped: credentials not configured",
            langfuse_host(),
            bool(_public_key()),
            bool(_secret_key()),
        )
        return False

    try:
        from opentelemetry import trace as trace_api
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        logger.info(
            "import_error=<%s> | Langfuse exporter skipped: "
            "opentelemetry-exporter-otlp-proto-http not installed",
            exc,
        )
        return False

    provider = trace_api.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        # The global provider is the default no-op one. Nothing would
        # ever be exported, so skip loudly rather than silently
        # dropping spans.
        logger.info(
            "provider_type=<%s> | Langfuse exporter skipped: "
            "no TracerProvider registered; call setup_otel() first",
            type(provider).__name__,
        )
        return False

    endpoint = f"{langfuse_host()}{_OTLP_TRACES_PATH}"
    auth = base64.b64encode(
        f"{_public_key()}:{_secret_key()}".encode("ascii")
    ).decode("ascii")
    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        headers={"Authorization": f"Basic {auth}"},
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _EXPORTER_INSTALLED = True
    logger.info(
        "endpoint=<%s>, host=<%s> | Langfuse OTel exporter installed",
        endpoint,
        langfuse_host(),
    )
    return True


def frontend_config() -> dict[str, Any]:
    """Return the Langfuse config slice safe for the frontend.

    The backend surfaces this on ``/playground/config/langfuse`` so
    the frontend can decide whether to render the "View Trace"
    button without having to call a separate healthcheck. Never
    leaks credentials — only the public host and whether the
    exporter is active.
    """
    host = langfuse_host() if is_langfuse_enabled() else None
    if host is not None:
        # Sanity-check the host parses; a misconfigured
        # ``LANGFUSE_HOST=foo`` (no scheme) would give the frontend
        # a junk URL. Fall back to None so the button is hidden.
        parsed = urlparse(host)
        if not parsed.scheme or not parsed.netloc:
            host = None
    return {
        "enabled": host is not None,
        "host": host,
    }
