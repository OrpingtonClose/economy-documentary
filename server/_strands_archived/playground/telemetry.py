"""OTel bootstrap for the Component Playground.

Slice 2 of the Wave 2/3 pipeline (``docs/strands-migration/plans/next-wave.md``).

Responsibilities:

* Ensure the global ``TracerProvider`` is set up for the playground
  process. The ADK path in ``server/plugins/__init__.py`` does the
  equivalent for the full pipeline, but the standalone
  ``playground_server`` intentionally does not pull ADK, so we need a
  parallel no-ADK-dependencies version here.
* Hand the provider to :func:`.langfuse.setup_langfuse_exporter` so
  spans get exported to Langfuse when configured. Graceful no-op when
  Langfuse is not configured.
* Expose :func:`playground_tracer` for ``_dispatch_run`` to open a
  root span per run. The root span carries the ``component_id`` /
  ``case_name`` / ``run_id`` attributes Langfuse uses to group and
  filter traces.

This module is import-safe: all OTel imports are deferred so a
``playground_server`` without the OTel SDK installed still boots (just
without tracing). Idempotent: re-entrant ``setup_playground_otel()``
calls only register the SDK provider once.
"""

from __future__ import annotations

import logging
from typing import Any

from strands_agents.playground.langfuse import (
    is_langfuse_enabled,
    setup_langfuse_exporter,
)

logger = logging.getLogger(__name__)

_OTEL_READY: bool = False
_TRACER: Any = None


def setup_playground_otel() -> bool:
    """Ensure a TracerProvider is installed and wire Langfuse if configured.

    Returns ``True`` when tracing (regardless of Langfuse) is available
    to callers, ``False`` when the OTel SDK is not importable. Safe to
    call repeatedly; subsequent calls reuse the provider installed on
    the first call.

    The provider is only installed if no non-default provider is
    already active, so integration with the ADK pipeline's
    ``setup_otel()`` (which installs its own provider earlier in the
    process lifetime) is automatic.
    """
    global _OTEL_READY, _TRACER

    if _OTEL_READY:
        setup_langfuse_exporter()
        return True

    try:
        from opentelemetry import trace as trace_api
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError as exc:
        logger.info(
            "import_error=<%s> | playground OTel disabled: "
            "opentelemetry SDK not installed",
            exc,
        )
        return False

    current = trace_api.get_tracer_provider()
    if isinstance(current, TracerProvider):
        # Someone (ADK's ``setup_otel``, a test harness, an earlier
        # call into this function) already installed a TracerProvider.
        # Reuse it — stacking two providers breaks OTel's
        # "one provider per process" invariant.
        provider = current
        logger.info(
            "provider_type=<%s> | reusing existing TracerProvider",
            type(provider).__name__,
        )
    else:
        provider = TracerProvider(
            resource=Resource.create({"service.name": "component-playground"}),
        )
        trace_api.set_tracer_provider(provider)
        logger.info("installed playground TracerProvider")

    _TRACER = provider.get_tracer("strands_agents.playground")
    _OTEL_READY = True

    if is_langfuse_enabled():
        setup_langfuse_exporter()

    return True


def playground_tracer() -> Any:
    """Return the playground's OTel ``Tracer``, setting it up lazily.

    Returns ``None`` when the OTel SDK is not available so callers can
    degrade gracefully with a simple ``if tracer is None:`` check.
    """
    if not _OTEL_READY:
        setup_playground_otel()
    return _TRACER
