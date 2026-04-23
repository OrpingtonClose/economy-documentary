"""Unit tests for the Langfuse OTel exporter wiring (slice 2).

The tests exercise:

* Env-var gating (``is_langfuse_enabled``).
* Host resolution with / without ``LANGFUSE_HOST``.
* Trace-URL formatting and malformed-id rejection.
* Exporter installation against a real ``TracerProvider``,
  including idempotency.
* Graceful degradation paths (unset keys, malformed host,
  default no-op tracer provider).
* ``frontend_config`` contract the UI depends on.

The exporter itself is instantiated against a throwaway
``TracerProvider``; we assert the span processor lands on the
provider rather than going through the network. Network
integration lives in ``tests_integ/`` (a future slice).
"""

from __future__ import annotations

import importlib

import pytest

from strands_agents.playground import langfuse as lf_module


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe cached exporter state and env vars between tests.

    ``_EXPORTER_INSTALLED`` is module-global by design — we want a
    single process to wire the exporter once — so every test needs
    a clean slate.
    """
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    importlib.reload(lf_module)


class TestIsLangfuseEnabled:
    def test_disabled_when_no_creds(self) -> None:
        assert lf_module.is_langfuse_enabled() is False

    def test_disabled_when_only_public(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
        assert lf_module.is_langfuse_enabled() is False

    def test_disabled_when_only_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
        assert lf_module.is_langfuse_enabled() is False

    def test_enabled_when_both_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
        assert lf_module.is_langfuse_enabled() is True

    def test_disabled_when_creds_are_whitespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "   ")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "\t")
        assert lf_module.is_langfuse_enabled() is False


class TestLangfuseHost:
    def test_defaults_to_cloud(self) -> None:
        assert lf_module.langfuse_host() == "https://cloud.langfuse.com"

    def test_respects_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LANGFUSE_HOST", "https://obs.example.com")
        assert lf_module.langfuse_host() == "https://obs.example.com"

    def test_strips_trailing_slash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGFUSE_HOST", "https://obs.example.com/")
        assert lf_module.langfuse_host() == "https://obs.example.com"


class TestTraceUrl:
    _VALID_TRACE_ID = "a" * 32

    def test_none_when_disabled(self) -> None:
        assert lf_module.langfuse_trace_url(self._VALID_TRACE_ID) is None

    def test_formats_url_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
        monkeypatch.setenv("LANGFUSE_HOST", "https://obs.example.com")
        url = lf_module.langfuse_trace_url(self._VALID_TRACE_ID)
        assert url == f"https://obs.example.com/trace/{self._VALID_TRACE_ID}"

    def test_rejects_short_trace_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
        assert lf_module.langfuse_trace_url("deadbeef") is None

    def test_rejects_non_hex_trace_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
        assert lf_module.langfuse_trace_url("z" * 32) is None

    def test_rejects_host_without_scheme(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scheme-less host must not yield a clickable URL.

        Mirrors the ``frontend_config`` gate — otherwise the frontend
        would render a "View Trace" button linking to
        ``obs.example.com/trace/...`` (a relative path), which the
        browser resolves against the playground origin.
        """
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
        monkeypatch.setenv("LANGFUSE_HOST", "obs.example.com")
        assert lf_module.langfuse_trace_url(self._VALID_TRACE_ID) is None


class TestFrontendConfig:
    def test_disabled_without_creds(self) -> None:
        cfg = lf_module.frontend_config()
        assert cfg == {"enabled": False, "host": None}

    def test_enabled_with_creds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
        monkeypatch.setenv("LANGFUSE_HOST", "https://obs.example.com")
        cfg = lf_module.frontend_config()
        assert cfg == {
            "enabled": True,
            "host": "https://obs.example.com",
        }

    def test_malformed_host_disables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
        # Missing scheme — frontend would otherwise try to open
        # "//foo/trace/..." and silently fail.
        monkeypatch.setenv("LANGFUSE_HOST", "obs.example.com")
        cfg = lf_module.frontend_config()
        assert cfg["enabled"] is False
        assert cfg["host"] is None


class TestSetupLangfuseExporter:
    """Exercise the real install path against a throwaway TracerProvider."""

    def test_skipped_without_creds(self) -> None:
        assert lf_module.setup_langfuse_exporter() is False

    def test_skipped_without_tracer_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no SDK ``TracerProvider`` registered, exporter must no-op.

        The default global provider is the API-level no-op one — wiring
        a span processor onto it silently drops spans.
        """
        pytest.importorskip("opentelemetry.sdk.trace")

        from opentelemetry import trace as trace_api
        from opentelemetry.trace import ProxyTracerProvider

        # Force the default no-op provider.
        monkeypatch.setattr(
            trace_api,
            "_TRACER_PROVIDER",
            ProxyTracerProvider(),
            raising=False,
        )
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")

        assert lf_module.setup_langfuse_exporter() is False

    def test_installs_processor_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Happy path — creds + SDK provider present, processor lands."""
        pytest.importorskip("opentelemetry.sdk.trace")
        pytest.importorskip(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter"
        )

        from opentelemetry import trace as trace_api
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider(
            resource=Resource.create({"service.name": "test"}),
        )
        monkeypatch.setattr(
            trace_api, "_TRACER_PROVIDER", provider, raising=False
        )
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
        monkeypatch.setenv("LANGFUSE_HOST", "https://obs.example.com")

        before = len(_active_processors(provider))
        assert lf_module.setup_langfuse_exporter() is True
        after = len(_active_processors(provider))
        assert after == before + 1

    def test_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("opentelemetry.sdk.trace")
        pytest.importorskip(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter"
        )

        from opentelemetry import trace as trace_api
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider(
            resource=Resource.create({"service.name": "test"}),
        )
        monkeypatch.setattr(
            trace_api, "_TRACER_PROVIDER", provider, raising=False
        )
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")

        assert lf_module.setup_langfuse_exporter() is True
        processor_count = len(_active_processors(provider))
        # Second call must not stack another processor.
        assert lf_module.setup_langfuse_exporter() is True
        assert len(_active_processors(provider)) == processor_count


def _active_processors(provider: object) -> list[object]:
    """Best-effort introspection of a ``TracerProvider``'s processors.

    OTel does not expose a public API for this, so we reach into the
    documented internal multi-processor. Guarded so a future SDK
    refactor surfaces a clear AttributeError rather than silently
    passing the test.
    """
    mp = getattr(provider, "_active_span_processor", None)
    if mp is None:
        raise AssertionError(
            "TracerProvider has no _active_span_processor attribute; "
            "OTel internals may have shifted — update the test"
        )
    processors = getattr(mp, "_span_processors", None)
    if processors is None:
        # Newer OTel versions keep the tuple under a different name.
        processors = getattr(mp, "_processors", None)
    if processors is None:
        raise AssertionError(
            "MultiSpanProcessor shape unknown — update the test"
        )
    return list(processors)
