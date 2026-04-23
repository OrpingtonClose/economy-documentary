"""Tests for ``strands_agents.playground.telemetry`` (slice 2).

The telemetry module is the playground-side OTel bootstrap. It must:

* Install a ``TracerProvider`` iff none is active.
* Reuse an already-installed non-default provider.
* Degrade gracefully when the OTel SDK is missing (returns ``False``;
  playground boots without tracing).
* Expose a ``Tracer`` via :func:`playground_tracer` once wired.
* Call :func:`langfuse.setup_langfuse_exporter` when Langfuse creds
  are present — but never crash when they aren't.
"""

from __future__ import annotations

import importlib

import pytest

from strands_agents.playground import langfuse as lf_module
from strands_agents.playground import telemetry as telemetry_module


@pytest.fixture(autouse=True)
def _reset_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    importlib.reload(lf_module)
    importlib.reload(telemetry_module)


def _reset_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow ``set_tracer_provider`` to win for the duration of a test.

    ``opentelemetry.trace`` gates ``set_tracer_provider`` behind an
    ``Once`` guard so a single process can only install one provider.
    In pytest the guard survives across test modules — the first test
    that sets a provider wins for the rest of the run. The test file
    needs the guarantee that its ``set_tracer_provider`` call actually
    lands, so we reset the ``Once.done`` flag for each test.
    """
    from opentelemetry import trace as trace_api

    once = getattr(trace_api, "_TRACER_PROVIDER_SET_ONCE", None)
    if once is not None and hasattr(once, "_done"):
        monkeypatch.setattr(once, "_done", False, raising=False)


def test_setup_playground_otel_installs_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk.trace")

    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import ProxyTracerProvider

    _reset_once(monkeypatch)
    monkeypatch.setattr(
        trace_api, "_TRACER_PROVIDER", ProxyTracerProvider(), raising=False
    )

    assert telemetry_module.setup_playground_otel() is True

    provider = trace_api.get_tracer_provider()
    assert isinstance(provider, TracerProvider)


def test_setup_playground_otel_reuses_existing_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk.trace")

    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    pre_installed = TracerProvider(
        resource=Resource.create({"service.name": "pre-existing"}),
    )
    monkeypatch.setattr(
        trace_api, "_TRACER_PROVIDER", pre_installed, raising=False
    )

    assert telemetry_module.setup_playground_otel() is True
    # The same provider instance is still in place — no stacking.
    assert trace_api.get_tracer_provider() is pre_installed


def test_setup_playground_otel_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk.trace")

    from opentelemetry import trace as trace_api
    from opentelemetry.trace import ProxyTracerProvider

    _reset_once(monkeypatch)
    monkeypatch.setattr(
        trace_api, "_TRACER_PROVIDER", ProxyTracerProvider(), raising=False
    )

    assert telemetry_module.setup_playground_otel() is True
    first = trace_api.get_tracer_provider()
    assert telemetry_module.setup_playground_otel() is True
    second = trace_api.get_tracer_provider()
    assert first is second


def test_playground_tracer_none_without_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the OTel SDK import fails, tracer() returns None cleanly."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("opentelemetry"):
            raise ImportError("forced: SDK missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    # Force the module into a fresh state so its cached ``_TRACER`` /
    # ``_OTEL_READY`` flags don't short-circuit the import error path.
    importlib.reload(telemetry_module)

    assert telemetry_module.playground_tracer() is None


def test_playground_tracer_returns_tracer_when_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk.trace")

    from opentelemetry import trace as trace_api
    from opentelemetry.trace import ProxyTracerProvider

    _reset_once(monkeypatch)
    monkeypatch.setattr(
        trace_api, "_TRACER_PROVIDER", ProxyTracerProvider(), raising=False
    )

    tracer = telemetry_module.playground_tracer()
    assert tracer is not None
    # Spans from this tracer must produce a real 128-bit id — the root
    # invariant the "View Trace" button depends on.
    with tracer.start_as_current_span("probe") as span:
        ctx = span.get_span_context()
        assert ctx.trace_id != 0
        assert len(format(ctx.trace_id, "032x")) == 32


def test_setup_playground_otel_wires_langfuse_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("opentelemetry.sdk.trace")
    pytest.importorskip(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    )

    from opentelemetry import trace as trace_api
    from opentelemetry.trace import ProxyTracerProvider

    _reset_once(monkeypatch)
    monkeypatch.setattr(
        trace_api, "_TRACER_PROVIDER", ProxyTracerProvider(), raising=False
    )
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    monkeypatch.setenv("LANGFUSE_HOST", "https://obs.example.com")

    calls: list[bool] = []
    real_setup = lf_module.setup_langfuse_exporter

    def _recording() -> bool:
        result = real_setup()
        calls.append(result)
        return result

    monkeypatch.setattr(lf_module, "setup_langfuse_exporter", _recording)
    monkeypatch.setattr(
        telemetry_module, "setup_langfuse_exporter", _recording
    )

    assert telemetry_module.setup_playground_otel() is True
    assert calls and calls[0] is True
