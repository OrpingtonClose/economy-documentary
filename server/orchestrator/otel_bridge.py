"""
OTel trace tree unification — propagate trace context between ADK and mcp-agent.

Both frameworks use OpenTelemetry but create separate trace trees. This module
bridges them so the full pipeline appears as a single unified trace:

    ADK Pipeline Session
      └── scenario_director (ADK Agent)
      └── audio_agent (ADK Agent)
      └── visual_director (ADK LoopAgent)
      └── production_agent (ADK CustomAgent)
            └── planning (OTel span from orchestrator)
            │     └── plan_generation (OTel span)
            │     └── plan_evaluation (OTel span)
            └── execution (OTel span)
            │     └── batch_1 (OTel span)
            │     └── batch_2 (OTel span)
            └── synthesis (OTel span)
      └── assembly_director (ADK Agent)

Usage:
    # At the start of ProductionAgent._run_async_impl:
    from orchestrator.otel_bridge import propagate_context, create_child_span

    parent_ctx = propagate_context()
    with create_child_span("production_planning", parent_ctx) as span:
        plan = await orchestrator._generate_verified_plan()
        span.set_attribute("plan.batches", len(plan.batches))
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)

# OTel imports — graceful degradation if not installed
try:
    from opentelemetry import context as otel_context
    from opentelemetry import trace
    from opentelemetry.context import Context
    from opentelemetry.trace import Span, SpanKind, StatusCode, Tracer

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False
    Context = Any  # type: ignore[misc,assignment]
    Span = Any  # type: ignore[misc,assignment]
    Tracer = Any  # type: ignore[misc,assignment]

_TRACER_NAME = "documentary-pipeline"


def get_tracer() -> Optional[Tracer]:
    """Get the pipeline tracer, or None if OTel is not available."""
    if not _HAS_OTEL:
        return None
    return trace.get_tracer(_TRACER_NAME)


def propagate_context() -> Optional[Context]:
    """Capture the current OTel context for propagation.

    Call this from inside an ADK agent's _run_async_impl to capture
    the ADK span context. Pass it to create_child_span() so the
    orchestrator spans nest under the ADK agent span.
    """
    if not _HAS_OTEL:
        return None
    return otel_context.get_current()


@contextmanager
def create_child_span(
    name: str,
    parent_context: Optional[Context] = None,
    kind: Any = None,
    attributes: Optional[dict[str, Any]] = None,
) -> Generator[Any, None, None]:
    """Create a child span that nests under the given parent context.

    If OTel is not available, yields a no-op object.

    Usage::

        parent_ctx = propagate_context()
        with create_child_span("planning", parent_ctx) as span:
            span.set_attribute("clips", 30)
            plan = generate_plan()
    """
    if not _HAS_OTEL:
        yield _NoOpSpan()
        return

    tracer = get_tracer()
    if tracer is None:
        yield _NoOpSpan()
        return

    span_kind = kind if kind is not None else SpanKind.INTERNAL

    ctx = parent_context or otel_context.get_current()
    with tracer.start_as_current_span(
        name,
        context=ctx,
        kind=span_kind,
        attributes=attributes or {},
    ) as span:
        try:
            yield span
        except Exception as e:
            span.set_status(StatusCode.ERROR, str(e))
            span.record_exception(e)
            raise


def record_event(
    span: Any,
    name: str,
    attributes: Optional[dict[str, Any]] = None,
) -> None:
    """Record an event on a span (safe if OTel unavailable)."""
    if _HAS_OTEL and hasattr(span, "add_event"):
        span.add_event(name, attributes=attributes or {})


def inject_context_headers(headers: Optional[dict] = None) -> dict:
    """Inject current trace context into HTTP headers for propagation.

    Use this when the backend calls GPU workers via HTTP — the trace
    context propagates so GPU worker spans nest under the pipeline span.

    Usage::

        headers = inject_context_headers()
        response = requests.post(worker_url, headers=headers, json=payload)
    """
    headers = dict(headers or {})
    if not _HAS_OTEL:
        return headers

    try:
        from opentelemetry.propagate import inject
        inject(headers)
    except Exception as e:
        logger.debug("OTel context injection failed: %s", e)

    return headers


def extract_context_from_headers(headers: dict) -> Optional[Context]:
    """Extract trace context from incoming HTTP headers.

    Use this on the GPU worker side to link spans back to the pipeline.

    Usage::

        parent_ctx = extract_context_from_headers(request.headers)
        with create_child_span("video_generation", parent_ctx) as span:
            ...
    """
    if not _HAS_OTEL:
        return None

    try:
        from opentelemetry.propagate import extract
        return extract(headers)
    except Exception as e:
        logger.debug("OTel context extraction failed: %s", e)
        return None


class _NoOpSpan:
    """No-op span for when OTel is not available."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def add_event(self, name: str, attributes: Optional[dict] = None) -> None:
        pass

    def set_status(self, status: Any, description: str = "") -> None:
        pass

    def record_exception(self, exception: Exception) -> None:
        pass


def instrument_production_agent() -> None:
    """Wire OTel trace propagation into the ProductionAgent.

    Call this once at startup. It patches the ProductionAgent's
    _run_async_impl to create properly nested spans.
    """
    try:
        from orchestrator.production_agent import ProductionAgent

        original_run = ProductionAgent._run_async_impl

        async def _traced_run(self, ctx):
            parent_ctx = propagate_context()
            with create_child_span(
                "production_agent",
                parent_ctx,
                attributes={"agent.name": self.name},
            ) as span:
                async for event in original_run(self, ctx):
                    # Extract event data for span attributes
                    if hasattr(event, "parts") and event.parts:
                        try:
                            import json
                            data = json.loads(event.parts[0].text)
                            event_type = data.get("event", "unknown")
                            record_event(span, event_type, data)
                        except (json.JSONDecodeError, AttributeError):
                            pass
                    yield event

        ProductionAgent._run_async_impl = _traced_run
        logger.info("OTel bridge: ProductionAgent instrumented for trace propagation")
    except Exception as e:
        logger.debug("OTel bridge: could not instrument ProductionAgent: %s", e)
