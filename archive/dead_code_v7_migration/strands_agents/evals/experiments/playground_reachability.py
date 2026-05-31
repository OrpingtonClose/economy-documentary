"""Strands-evals experiment for the model-reachability probe.

PR 2 of ``docs/strands-migration/plans/component-playground.md``.
Exercises :class:`CredentialsProber` and
:class:`ReachabilityCache` end-to-end without hitting real
providers — the prober is injectable, so the experiment swaps in
deterministic environment stubs.

The hard invariant being protected:

    > Not being able to access the model is an automatic test failure.

Every case here asserts one consequence of that invariant:

* a provider with no credential configured returns
  ``reachable=False`` with reason ``no_credentials``;
* a provider with its credential configured returns
  ``reachable=True`` with reason ``ok``;
* an unrecognised provider returns ``reachable=False`` with reason
  ``unknown_provider``;
* the cache returns the same ``checked_at`` within the TTL and
  re-probes after the TTL expires;
* the ``MODEL_UNREACHABLE`` sentinel is stable.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case  # type: ignore[import-not-found]
from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.experiment import Experiment  # type: ignore[import-not-found]

from strands_agents.evals.experiments.playground_catalog import (
    SubsetMatchEvaluator,
)
from strands_agents.playground.reachability import (
    MODEL_UNREACHABLE,
    CredentialsProber,
    ReachabilityCache,
)
from strands_agents.playground.registry import DeclaredModel


#: Reuses the catalog's deterministic subset evaluator. Both gates
#: here are hard — the whole point of PR 2 is that model
#: unreachability is a hard failure.
PLAYGROUND_REACHABILITY_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "SubsetMatchEvaluator": (1.0, True),
}


_TEST_GEMINI: DeclaredModel = DeclaredModel(
    id="gemini/test-model", provider="gemini", role="canonical"
)
_TEST_OPENAI: DeclaredModel = DeclaredModel(
    id="openai/test-model", provider="openai", role="canonical"
)
_TEST_LOCAL: DeclaredModel = DeclaredModel(
    id="local/test-model", provider="local", role="candidate"
)
_TEST_MOONSHOT: DeclaredModel = DeclaredModel(
    id="moonshot/test-model", provider="moonshot", role="candidate"
)
_TEST_UNKNOWN: DeclaredModel = DeclaredModel(
    id="martian/test-model", provider="martian", role="candidate"
)


def _prober_with_env(env: dict[str, str]) -> CredentialsProber:
    return CredentialsProber(environ=lambda env=env: dict(env))


def playground_reachability_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Dispatch a case's scenario and return the resulting status.

    Each case's ``input`` names a scenario and carries its own stub
    environment. The scenarios map 1-to-1 to the hard invariants the
    experiment protects; keeping them named makes failure diagnostics
    greppable.
    """
    scenario = case.input["scenario"]
    env: dict[str, str] = case.input.get("env", {})
    model_data: dict[str, str] = case.input["model"]
    model = DeclaredModel(
        id=model_data["id"],
        provider=model_data["provider"],
        role=model_data["role"],
    )

    if scenario == "probe":
        prober = _prober_with_env(env)
        status = prober.probe(model)
        return {
            "output": {
                "reachable": status.reachable,
                "reason": status.reason,
                "model_id": status.model_id,
                "provider": status.provider,
            }
        }

    if scenario == "cache_hit_within_ttl":
        # Same probe twice within TTL → same checked_at.
        call_counter = {"n": 0}

        class CountingProber:
            def probe(self, model: DeclaredModel):
                call_counter["n"] += 1
                return _prober_with_env(env).probe(model)

        cache = ReachabilityCache(CountingProber(), ttl_seconds=60.0)
        first = cache.get(model)
        second = cache.get(model)
        return {
            "output": {
                "probe_calls": call_counter["n"],
                "same_timestamp": first.checked_at == second.checked_at,
                "reachable": first.reachable,
            }
        }

    if scenario == "cache_miss_after_ttl":
        # Same probe twice across TTL → different checked_at, prober
        # called twice.
        call_counter = {"n": 0}

        class CountingProber2:
            def probe(self, model: DeclaredModel):
                call_counter["n"] += 1
                return _prober_with_env(env).probe(model)

        # Use a tick-based clock so the second call falls strictly
        # after the entry's expiry. ``ReachabilityCache.get`` consults
        # ``clock`` once per call, so two ticks cover two ``get``s.
        ticks = iter([0.0, 1000.0])
        cache = ReachabilityCache(
            CountingProber2(), ttl_seconds=10.0, clock=lambda t=ticks: next(t)
        )
        cache.get(model)
        cache.get(model)
        return {"output": {"probe_calls": call_counter["n"]}}

    if scenario == "sentinel_is_stable":
        return {"output": {"sentinel": MODEL_UNREACHABLE}}

    raise ValueError(f"unknown scenario: {scenario}")


def _probe_case(
    name: str,
    model: DeclaredModel,
    env: dict[str, str],
    expected: dict[str, Any],
) -> Case[dict[str, Any], dict[str, Any]]:
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"reachability-{name}",
        input={
            "scenario": "probe",
            "env": env,
            "model": {"id": model.id, "provider": model.provider, "role": model.role},
        },
        expected_output=expected,
    )


def playground_reachability_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Build the case corpus for PR 2."""
    return [
        _probe_case(
            "gemini_with_key_reachable",
            _TEST_GEMINI,
            {"GEMINI_API_KEY": "sk-test"},
            {"reachable": True, "reason": "ok", "provider": "gemini"},
        ),
        _probe_case(
            "gemini_google_api_key_also_accepted",
            _TEST_GEMINI,
            {"GOOGLE_API_KEY": "sk-test"},
            {"reachable": True, "reason": "ok"},
        ),
        _probe_case(
            "gemini_no_key_unreachable",
            _TEST_GEMINI,
            {},
            {"reachable": False, "reason": "no_credentials"},
        ),
        _probe_case(
            "openai_with_key_reachable",
            _TEST_OPENAI,
            {"OPENAI_API_KEY": "sk-test"},
            {"reachable": True, "reason": "ok"},
        ),
        _probe_case(
            "openai_no_key_unreachable",
            _TEST_OPENAI,
            {},
            {"reachable": False, "reason": "no_credentials"},
        ),
        _probe_case(
            "moonshot_kimi_key_accepted",
            _TEST_MOONSHOT,
            {"KIMI_API_KEY": "sk-test"},
            {"reachable": True, "reason": "ok"},
        ),
        _probe_case(
            "local_endpoint_configured_reachable",
            _TEST_LOCAL,
            {"OLLAMA_HOST": "http://localhost:11434"},
            {"reachable": True, "reason": "ok"},
        ),
        _probe_case(
            "local_no_endpoint_unreachable",
            _TEST_LOCAL,
            {},
            {"reachable": False, "reason": "no_credentials"},
        ),
        _probe_case(
            "unknown_provider_unreachable",
            _TEST_UNKNOWN,
            {"MARTIAN_API_KEY": "sk-test"},
            {"reachable": False, "reason": "unknown_provider"},
        ),
        Case[dict[str, Any], dict[str, Any]](
            name="cache_hit_within_ttl_reuses_result",
            session_id="reachability-cache-hit",
            input={
                "scenario": "cache_hit_within_ttl",
                "env": {"OPENAI_API_KEY": "sk-test"},
                "model": {
                    "id": _TEST_OPENAI.id,
                    "provider": _TEST_OPENAI.provider,
                    "role": _TEST_OPENAI.role,
                },
            },
            expected_output={
                "probe_calls": 1,
                "same_timestamp": True,
                "reachable": True,
            },
        ),
        Case[dict[str, Any], dict[str, Any]](
            name="cache_miss_after_ttl_re_probes",
            session_id="reachability-cache-miss",
            input={
                "scenario": "cache_miss_after_ttl",
                "env": {"OPENAI_API_KEY": "sk-test"},
                "model": {
                    "id": _TEST_OPENAI.id,
                    "provider": _TEST_OPENAI.provider,
                    "role": _TEST_OPENAI.role,
                },
            },
            expected_output={"probe_calls": 2},
        ),
        Case[dict[str, Any], dict[str, Any]](
            name="sentinel_is_canonical_string",
            session_id="reachability-sentinel",
            input={
                "scenario": "sentinel_is_stable",
                "model": {
                    "id": "n/a",
                    "provider": "n/a",
                    "role": "canonical",
                },
            },
            expected_output={"sentinel": "MODEL_UNREACHABLE"},
        ),
    ]


def playground_reachability_evaluators() -> list[Evaluator[Any, Any]]:
    return [SubsetMatchEvaluator()]


def build_playground_reachability_experiment() -> Experiment[Any, Any]:
    return Experiment(
        cases=playground_reachability_cases(),
        evaluators=playground_reachability_evaluators(),
    )


__all__ = ["build_playground_reachability_experiment",
    "playground_reachability_cases",
    "playground_reachability_evaluators",
    "playground_reachability_task",]
