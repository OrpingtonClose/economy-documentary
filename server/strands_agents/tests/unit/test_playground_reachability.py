"""Unit tests for the model-reachability probe (PR 2)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from playground import router as playground_router
from strands_agents.evals.experiments.playground_reachability import (
    PLAYGROUND_REACHABILITY_EVALUATOR_THRESHOLDS,
    build_playground_reachability_experiment,
    playground_reachability_task,
)
from strands_agents.playground.reachability import (
    MODEL_UNREACHABLE,
    CredentialsProber,
    LiteLLMPingProber,
    ReachabilityCache,
    set_default_cache,
)
from strands_agents.playground.registry import DeclaredModel


def test_reachability_experiment_passes_every_case() -> None:
    exp = build_playground_reachability_experiment()
    reports = exp.run_evaluations(task=playground_reachability_task)
    assert len(reports) == 1
    report = reports[0]
    assert all(report.test_passes), [
        (case["name"], reason)
        for case, passed, reason in zip(
            report.cases, report.test_passes, report.reasons
        )
        if not passed
    ]
    threshold, hard_gate = PLAYGROUND_REACHABILITY_EVALUATOR_THRESHOLDS[
        report.evaluator_name
    ]
    assert hard_gate is True
    assert report.overall_score >= threshold


def test_models_health_endpoint_reports_unreachable_without_credentials() -> None:
    # Install a cache with an empty environment so every declared
    # model probes as unreachable — deterministic regardless of host
    # configuration.
    empty_env_prober = CredentialsProber(environ=lambda: {})
    set_default_cache(ReachabilityCache(empty_env_prober))
    try:
        app = FastAPI()
        app.include_router(playground_router)
        client = TestClient(app)
        response = client.get("/playground/components/c01/models/health")
        assert response.status_code == 200
        body = response.json()
        assert body["component_id"] == "c01"
        assert body["total"] == 3  # Gemini + GPT-4o + Kimi
        assert body["all_reachable"] is False
        assert body["unreachable_sentinel"] == MODEL_UNREACHABLE
        for entry in body["models"]:
            assert entry["reachable"] is False
            assert entry["reason"] in {"no_credentials", "unknown_provider"}
    finally:
        # Restore the process-wide default so subsequent tests aren't
        # poisoned by the empty-env cache.
        set_default_cache(ReachabilityCache(CredentialsProber()))


def test_models_health_endpoint_reports_reachable_when_credentials_present() -> None:
    fake_env = {
        "GEMINI_API_KEY": "sk-test",
        "OPENAI_API_KEY": "sk-test",
        "KIMI_API_KEY": "sk-test",
    }
    set_default_cache(ReachabilityCache(CredentialsProber(environ=lambda: fake_env)))
    try:
        app = FastAPI()
        app.include_router(playground_router)
        client = TestClient(app)
        response = client.get("/playground/components/c01/models/health")
        assert response.status_code == 200
        body = response.json()
        assert body["all_reachable"] is True
        assert all(entry["reason"] == "ok" for entry in body["models"])
    finally:
        set_default_cache(ReachabilityCache(CredentialsProber()))


def test_all_models_health_endpoint_dedupes_shared_models() -> None:
    set_default_cache(ReachabilityCache(CredentialsProber(environ=lambda: {})))
    try:
        app = FastAPI()
        app.include_router(playground_router)
        client = TestClient(app)
        response = client.get("/playground/models/health")
        assert response.status_code == 200
        body = response.json()
        seen_ids = {m["model_id"] for m in body["models"]}
        assert len(seen_ids) == body["total"]
        # The five module-level DeclaredModel constants in registry.py:
        # Gemini 3.1, Gemma 4 uncensored, Qwen3.5-Omni, GPT-4o, Kimi K2.
        assert body["total"] == 5
    finally:
        set_default_cache(ReachabilityCache(CredentialsProber()))


def test_models_health_returns_404_for_unknown_component() -> None:
    app = FastAPI()
    app.include_router(playground_router)
    client = TestClient(app)
    response = client.get("/playground/components/c99/models/health")
    assert response.status_code == 404


def test_ping_prober_reports_reachable_when_complete_returns() -> None:
    calls: list[dict] = []

    def fake_complete(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    prober = LiteLLMPingProber(complete=fake_complete)
    status = prober.probe(
        DeclaredModel(id="gemini/gemini-3-pro-preview", provider="gemini", role="canonical")
    )
    assert status.reachable is True
    assert status.reason == "ok"
    assert status.latency_ms is not None
    assert calls and calls[0]["model"] == "gemini/gemini-3-pro-preview"
    assert calls[0]["max_tokens"] == 1


def test_ping_prober_reports_unreachable_on_exception() -> None:
    def fake_complete(**_: object) -> object:
        raise RuntimeError("404 NOT_FOUND: models/x not found for API version v1alpha")

    prober = LiteLLMPingProber(complete=fake_complete)
    status = prober.probe(
        DeclaredModel(id="gemini/x", provider="gemini", role="canonical")
    )
    assert status.reachable is False
    assert status.reason.startswith("probe_error:RuntimeError:")
    assert "404 NOT_FOUND" in status.reason


def test_ping_prober_truncates_long_error_messages() -> None:
    long_tail = "x" * 500

    def fake_complete(**_: object) -> object:
        raise ValueError(long_tail)

    prober = LiteLLMPingProber(complete=fake_complete)
    status = prober.probe(
        DeclaredModel(id="openai/x", provider="openai", role="canonical")
    )
    # Prefix is ``probe_error:ValueError: ``; the message itself is
    # trimmed to the 160-char cap plus an ellipsis, so the final
    # reason stays comfortably under 200 chars.
    assert len(status.reason) < 200
    assert status.reason.endswith("...")


def test_cache_invalidate_clears_results() -> None:
    call_count = {"n": 0}

    class CountingProber:
        def probe(self, model: DeclaredModel):  # type: ignore[no-untyped-def]
            call_count["n"] += 1
            return CredentialsProber(environ=lambda: {"OPENAI_API_KEY": "sk"}).probe(
                model
            )

    cache = ReachabilityCache(CountingProber(), ttl_seconds=3600.0)
    model = DeclaredModel(id="openai/x", provider="openai", role="canonical")
    cache.get(model)
    cache.get(model)
    assert call_count["n"] == 1
    cache.invalidate()
    cache.get(model)
    assert call_count["n"] == 2
