"""Tests for :mod:`critique.critic_squad`.

These tests target the ADK-independent pieces: JSON extraction, the
after_agent callback, and :class:`CriticSpec` defaults.  The
:func:`build_critic_squad` factory itself requires ``google.adk`` at
call time and is only exercised via monkey-patching so the test suite
can run without the ADK installed.
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any

import pytest

_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from critique.critic_squad import (  # noqa: E402
    CriticSpec,
    _extract_json_payload,
    _safe_state_get,
    build_critic_squad,
    make_critic_squad_callback,
)
from critique.store import ArtifactCritiqueStore  # noqa: E402


# ---------------------------------------------------------------------------
# _extract_json_payload
# ---------------------------------------------------------------------------

def test_extract_json_payload_dict_passthrough():
    assert _extract_json_payload({"rating": "GOOD"}) == {"rating": "GOOD"}


def test_extract_json_payload_plain_json_string():
    assert _extract_json_payload('{"rating": "FAIR"}') == {"rating": "FAIR"}


def test_extract_json_payload_wrapped_in_prose():
    payload = _extract_json_payload(
        "Here is my review:\n{\n  \"rating\": \"POOR\",\n  \"summary\": \"bad\"\n}\nThanks."
    )
    assert payload == {"rating": "POOR", "summary": "bad"}


def test_extract_json_payload_markdown_fence():
    payload = _extract_json_payload(
        "```json\n{\"rating\": \"EXCELLENT\", \"score\": 0.9}\n```"
    )
    assert payload == {"rating": "EXCELLENT", "score": 0.9}


def test_extract_json_payload_returns_none_for_non_object():
    assert _extract_json_payload(None) is None
    assert _extract_json_payload("") is None
    assert _extract_json_payload("just prose, no braces") is None
    assert _extract_json_payload("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# _safe_state_get
# ---------------------------------------------------------------------------

def test_safe_state_get_mapping_and_attribute():
    mapping_state = {"foo": 1}
    assert _safe_state_get(mapping_state, "foo") == 1
    assert _safe_state_get(mapping_state, "missing") is None

    attr_state = types.SimpleNamespace(bar=2)
    assert _safe_state_get(attr_state, "bar") == 2
    assert _safe_state_get(attr_state, "missing") is None


# ---------------------------------------------------------------------------
# CriticSpec defaults
# ---------------------------------------------------------------------------

def test_critic_spec_defaults_resolve_consistently():
    model = types.SimpleNamespace(model="gemini-2.5-flash")
    spec = CriticSpec(name="scenario_critic", model=model, instruction="do it")
    assert spec.resolved_output_key() == "critique_scenario_critic"
    assert spec.resolved_voter_model() == "gemini-2.5-flash"
    assert spec.resolved_source() == "scenario_critic"


def test_critic_spec_overrides_respect_explicit_values():
    model = types.SimpleNamespace(model="ignored")
    spec = CriticSpec(
        name="brand_voice",
        model=model,
        instruction="...",
        output_key="brand_voice_critique_output",
        voter_model="claude-3.5",
        critic_source="brand_voice_v2",
    )
    assert spec.resolved_output_key() == "brand_voice_critique_output"
    assert spec.resolved_voter_model() == "claude-3.5"
    assert spec.resolved_source() == "brand_voice_v2"


# ---------------------------------------------------------------------------
# Callback behaviour
# ---------------------------------------------------------------------------

def _make_cb_context(state: dict[str, Any]):
    return types.SimpleNamespace(state=state)


@pytest.fixture()
def store(tmp_path) -> ArtifactCritiqueStore:
    return ArtifactCritiqueStore(root=tmp_path, b2_enabled=False)


def test_callback_persists_critiques_from_each_critic(store):
    critics = [
        CriticSpec(
            name="scenario_critic",
            model=types.SimpleNamespace(model="gemini-2.5-flash"),
            instruction="...",
        ),
        CriticSpec(
            name="style_critic",
            model=types.SimpleNamespace(model="gemini-2.5-flash"),
            instruction="...",
        ),
    ]
    callback = make_critic_squad_callback(
        critics=critics,
        artifact_resolver=lambda state: ("scenario", state["scenario_id"]),
        store=store,
        produced_by="scenario_critic_squad",
    )

    state = {
        "scenario_id": "run-001",
        "critique_scenario_critic": '{"rating": "GOOD", "summary": "ok", "issues": []}',
        "critique_style_critic": (
            "```json\n{\"rating\": \"FAIR\", \"summary\": \"voice flat\", "
            "\"suggestions\": [\"vary cadence\"]}\n```"
        ),
    }
    callback(_make_cb_context(state))

    rec = store.read("scenario", "run-001")
    assert rec is not None
    assert len(rec.critiques) == 2
    sources = {c.source for c in rec.critiques}
    assert sources == {"scenario_critic", "style_critic"}
    ratings = {c.source: c.rating for c in rec.critiques}
    assert ratings["scenario_critic"] == "GOOD"
    assert ratings["style_critic"] == "FAIR"


def test_callback_skips_critic_with_malformed_output(store):
    critics = [
        CriticSpec(
            name="good_critic",
            model=types.SimpleNamespace(model="g"),
            instruction="...",
        ),
        CriticSpec(
            name="bad_critic",
            model=types.SimpleNamespace(model="g"),
            instruction="...",
        ),
    ]
    callback = make_critic_squad_callback(
        critics=critics,
        artifact_resolver=lambda state: ("scene", state["scene_id"]),
        store=store,
    )

    state = {
        "scene_id": "s001",
        "critique_good_critic": '{"rating": "EXCELLENT", "summary": "great"}',
        "critique_bad_critic": "I refuse to follow format",
    }
    callback(_make_cb_context(state))

    rec = store.read("scene", "s001")
    assert rec is not None
    assert len(rec.critiques) == 1
    assert rec.critiques[0].source == "good_critic"


def test_callback_swallows_resolver_errors(store):
    critics = [
        CriticSpec(
            name="c",
            model=types.SimpleNamespace(model="g"),
            instruction="...",
        ),
    ]
    def _boom(_state: Any) -> tuple[str, str]:
        raise KeyError("scene_id missing")

    callback = make_critic_squad_callback(
        critics=critics,
        artifact_resolver=_boom,
        store=store,
    )
    # Must not raise — just silently no-op.
    callback(_make_cb_context({"critique_c": '{"rating": "GOOD"}'}))

    # Nothing should be persisted; list_ids returns empty.
    assert store.list_ids() == []


def test_callback_skips_when_identity_blank(store):
    critics = [
        CriticSpec(
            name="c",
            model=types.SimpleNamespace(model="g"),
            instruction="...",
        ),
    ]
    callback = make_critic_squad_callback(
        critics=critics,
        artifact_resolver=lambda _state: ("scenario", ""),
        store=store,
    )
    callback(_make_cb_context({"critique_c": '{"rating": "GOOD"}'}))
    assert store.list_ids() == []


def test_callback_passes_iteration_when_resolver_present(store):
    critics = [
        CriticSpec(
            name="scenario_critic",
            model=types.SimpleNamespace(model="g"),
            instruction="...",
        ),
    ]
    callback = make_critic_squad_callback(
        critics=critics,
        artifact_resolver=lambda state: ("scenario", state["scenario_id"]),
        store=store,
        iteration_resolver=lambda state: state.get("iteration"),
    )

    state = {
        "scenario_id": "run-001",
        "iteration": 3,
        "critique_scenario_critic": '{"rating": "GOOD"}',
    }
    callback(_make_cb_context(state))

    rec = store.read("scenario", "run-001")
    assert rec is not None
    assert rec.iteration >= 3


# ---------------------------------------------------------------------------
# build_critic_squad (lazy ADK import path) — exercised via stubbing
# ---------------------------------------------------------------------------

def test_build_critic_squad_requires_at_least_one_critic(store):
    with pytest.raises(ValueError, match="at least one critic"):
        build_critic_squad(
            name="squad",
            description="test",
            critics=[],
            artifact_resolver=lambda _state: ("scenario", "x"),
            store=store,
        )


def test_build_critic_squad_wires_parallel_agent_via_stubs(monkeypatch, store):
    """Stub out ``google.adk.agents.*`` so we don't need the real ADK
    installed to validate the factory wires ``Agent`` + ``ParallelAgent``
    with the expected arguments.
    """

    created_agents: list[dict[str, Any]] = []
    created_parallels: list[dict[str, Any]] = []

    class _StubAgent:
        def __init__(self, **kw):
            created_agents.append(kw)
            self.name = kw.get("name")
            self.output_key = kw.get("output_key")

    class _StubParallel:
        def __init__(self, **kw):
            created_parallels.append(kw)
            self.name = kw.get("name")
            self.sub_agents = kw.get("sub_agents") or []
            self.after_agent_callback = kw.get("after_agent_callback")

    # Build an in-memory ``google.adk.agents`` + ``.parallel_agent`` module
    # graph so the lazy import inside ``build_critic_squad`` resolves.
    google_mod = types.ModuleType("google")
    adk_mod = types.ModuleType("google.adk")
    agents_mod = types.ModuleType("google.adk.agents")
    parallel_mod = types.ModuleType("google.adk.agents.parallel_agent")

    agents_mod.Agent = _StubAgent  # type: ignore[attr-defined]
    parallel_mod.ParallelAgent = _StubParallel  # type: ignore[attr-defined]
    agents_mod.parallel_agent = parallel_mod  # type: ignore[attr-defined]
    adk_mod.agents = agents_mod  # type: ignore[attr-defined]
    google_mod.adk = adk_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.adk", adk_mod)
    monkeypatch.setitem(sys.modules, "google.adk.agents", agents_mod)
    monkeypatch.setitem(
        sys.modules, "google.adk.agents.parallel_agent", parallel_mod,
    )

    critics = [
        CriticSpec(
            name="scenario_critic",
            model=types.SimpleNamespace(model="gemini-2.5-flash"),
            instruction="Rate the scenario.",
            description="Narrative critic",
        ),
        CriticSpec(
            name="style_critic",
            model=types.SimpleNamespace(model="gemini-2.5-flash"),
            instruction="Rate the style.",
        ),
    ]

    squad = build_critic_squad(
        name="scenario_critic_squad",
        description="Post-scenario critique squad",
        critics=critics,
        artifact_resolver=lambda state: ("scenario", state["scenario_id"]),
        store=store,
        produced_by="scenario_critic_squad",
    )

    assert squad.name == "scenario_critic_squad"
    assert len(created_agents) == 2
    assert [a["name"] for a in created_agents] == ["scenario_critic", "style_critic"]
    assert created_agents[0]["output_key"] == "critique_scenario_critic"
    assert len(created_parallels) == 1
    assert created_parallels[0]["name"] == "scenario_critic_squad"
    assert created_parallels[0]["after_agent_callback"] is squad.callback

    # Callback still works when invoked directly.
    state = {
        "scenario_id": "run-xyz",
        "critique_scenario_critic": '{"rating": "GOOD"}',
        "critique_style_critic": '{"rating": "FAIR"}',
    }
    squad.callback(types.SimpleNamespace(state=state))
    rec = store.read("scenario", "run-xyz")
    assert rec is not None
    assert {c.source for c in rec.critiques} == {"scenario_critic", "style_critic"}
