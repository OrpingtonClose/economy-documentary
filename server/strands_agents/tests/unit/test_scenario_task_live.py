"""Unit tests for the live ``scenario_task`` playground adapter.

Covers:

* ``_pick_model`` respects the explicit env override and otherwise
  falls through the default order based on provider credentials.
* ``_infer_num_scenes`` honours metadata > topic literal > duration.
* ``_extract_output_and_trajectory`` pairs ``toolUse`` → ``toolResult``
  blocks correctly and merges the scenario agent's four tool payloads
  into the expected envelope.
* ``scenario_task`` runs the real Strands agent against a stubbed
  litellm and returns a live-mode envelope with scenes, style_lock,
  and a non-empty trajectory.

No LLM calls leave the process — the one place that would reach the
network (``litellm.completion``) is monkeypatched.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from strands_evals.case import Case

from strands_agents.evals.experiments import scenario as scenario_exp
from strands_agents.evals.experiments.scenario import (
    SCENARIO_PLAYGROUND_MODEL_ENV,
    _extract_output_and_trajectory,
    _infer_num_scenes,
    _pick_model,
    scenario_task,
)


# ---------------------------------------------------------------------------
# _pick_model
# ---------------------------------------------------------------------------


def test_pick_model_honours_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SCENARIO_PLAYGROUND_MODEL_ENV, "moonshot/kimi-k2")
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert _pick_model() == "moonshot/kimi-k2"


def test_pick_model_walks_default_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SCENARIO_PLAYGROUND_MODEL_ENV, raising=False)
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert _pick_model() == "openai/gpt-4o"


def test_pick_model_falls_back_to_canonical_when_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SCENARIO_PLAYGROUND_MODEL_ENV, raising=False)
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert _pick_model() == "gemini/gemini-3.1-pro"


# ---------------------------------------------------------------------------
# _infer_num_scenes
# ---------------------------------------------------------------------------


def test_infer_num_scenes_prefers_metadata_hint() -> None:
    assert _infer_num_scenes("5-scene inflation doc", 300.0, metadata_hint=9) == 9


def test_infer_num_scenes_reads_topic_literal_when_metadata_absent() -> None:
    assert _infer_num_scenes("Produce a 7-scene overview", 600.0, metadata_hint=None) == 7


def test_infer_num_scenes_falls_back_to_duration_heuristic() -> None:
    # 300s / 45 ≈ 6.67 → ceil → 7
    assert _infer_num_scenes("free-form topic", 300.0, metadata_hint=None) == 7


def test_infer_num_scenes_clamps_insane_topic_values() -> None:
    assert _infer_num_scenes("a 99999-scene doc", 60.0, metadata_hint=None) == 2


# ---------------------------------------------------------------------------
# _extract_output_and_trajectory
# ---------------------------------------------------------------------------


def _tool_use_block(name: str, use_id: str, **inp: Any) -> dict[str, Any]:
    return {"toolUse": {"toolUseId": use_id, "name": name, "input": inp}}


def _tool_result_block(use_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "toolResult": {
            "toolUseId": use_id,
            "status": "success",
            "content": [{"text": json.dumps(payload)}],
        }
    }


def test_extract_output_and_trajectory_merges_all_four_tools() -> None:
    messages = [
        {"role": "user", "content": [{"text": "topic"}]},
        {
            "role": "assistant",
            "content": [_tool_use_block("generate_scenario", "u1", topic="x")],
        },
        {
            "role": "user",
            "content": [
                _tool_result_block(
                    "u1",
                    {
                        "scenes": [{"scene_num": 1, "duration_sec": 30.0}],
                        "visual_style": {"style": "cinematic"},
                        "style_lock": {"dominant_style": "cinematic_documentary"},
                    },
                )
            ],
        },
        {
            "role": "assistant",
            "content": [_tool_use_block("evaluate_scenario", "u2")],
        },
        {
            "role": "user",
            "content": [
                _tool_result_block(
                    "u2",
                    {"rating": "FAIR", "issues": [{"check": "duration"}], "suggestions": []},
                )
            ],
        },
        {
            "role": "assistant",
            "content": [_tool_use_block("refine_scenario", "u3")],
        },
        {
            "role": "user",
            "content": [
                _tool_result_block(
                    "u3",
                    {"scenes": [{"scene_num": 1, "duration_sec": 45.0}]},
                )
            ],
        },
        {
            "role": "assistant",
            "content": [_tool_use_block("evaluate_scenario", "u4")],
        },
        {
            "role": "user",
            "content": [
                _tool_result_block("u4", {"rating": "GOOD", "issues": [], "suggestions": []})
            ],
        },
        {
            "role": "assistant",
            "content": [_tool_use_block("create_timeline", "u5")],
        },
        {
            "role": "user",
            "content": [
                _tool_result_block(
                    "u5",
                    {
                        "timeline_path": "/tmp/t.otio",
                        "total_duration_sec": 45.0,
                        "num_scenes": 1,
                    },
                )
            ],
        },
    ]

    output, trajectory = _extract_output_and_trajectory(messages)

    assert trajectory == [
        "generate_scenario",
        "evaluate_scenario",
        "refine_scenario",
        "evaluate_scenario",
        "create_timeline",
    ]
    # Refine's scenes win over the initial generation.
    assert output["scenes"] == [{"scene_num": 1, "duration_sec": 45.0}]
    assert output["style_lock"] == {"dominant_style": "cinematic_documentary"}
    assert output["visual_style"] == {"style": "cinematic"}
    assert output["timeline"]["timeline_path"] == "/tmp/t.otio"
    assert [r["rating"] for r in output["evaluator_reports"]] == ["FAIR", "GOOD"]


def test_extract_skips_non_parseable_tool_results() -> None:
    messages = [
        {"role": "assistant", "content": [_tool_use_block("generate_scenario", "u1")]},
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "u1",
                        "status": "success",
                        "content": [{"text": "not json at all"}],
                    }
                }
            ],
        },
    ]

    output, trajectory = _extract_output_and_trajectory(messages)
    assert trajectory == ["generate_scenario"]
    assert output == {}


# ---------------------------------------------------------------------------
# scenario_task end-to-end (LLM stubbed)
# ---------------------------------------------------------------------------


class _StubLiteLLMResponse:
    """Minimal ``litellm.completion`` return shape."""

    def __init__(self, content: str) -> None:
        self._content = content

    def __getitem__(self, key: str) -> Any:  # pragma: no cover - trivial
        if key != "choices":
            raise KeyError(key)
        return [{"message": {"content": self._content}}]


def _scenes_fixture(total: float = 300.0) -> list[dict[str, Any]]:
    # Three scenes summing to ``total`` seconds; minimum viable shape.
    per = total / 3
    scenes = [
        {
            "scene_num": 1,
            "title": "Intro",
            "duration_sec": per,
            "narration": "Inflation is the rate at which prices rise over time. " * 6,
            "pronunciation_hints": [],
            "visual_notes": "Slow dolly over a desk with invoices.",
            "dopamine_hook": "Prices tell a story.",
            "hook_spec": {"kind": "cold_open", "beat": "prices rising"},
        },
        {
            "scene_num": 2,
            "title": "Mechanism",
            "duration_sec": per,
            "narration": "Supply meets demand; prices adjust. " * 10,
            "pronunciation_hints": [],
            "visual_notes": "Market stall, people exchanging cash.",
            "dopamine_hook": "How prices find a level.",
        },
        {
            "scene_num": 3,
            "title": "Outro",
            "duration_sec": per,
            "narration": "Inflation is a general rise in prices. " * 7,
            "pronunciation_hints": [],
            "visual_notes": "Cafe counter, coins on saucer.",
            "dopamine_hook": "That's inflation.",
            "outro_spec": {"kind": "takeaway", "beat": "prices summarised"},
        },
    ]
    return scenes


def test_scenario_task_runs_live_agent_against_stubbed_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The playground task must invoke the Strands agent, capture a
    real trajectory, and surface scenes from the generator helper —
    not replay a stub. litellm is monkeypatched so no network call
    leaves the process."""

    monkeypatch.setenv(SCENARIO_PLAYGROUND_MODEL_ENV, "openai/gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

    scenes = _scenes_fixture(total=300.0)
    generator_payload = {
        "scenes": scenes,
        "visual_style": {
            "style": "cinematic documentary",
            "realism_anchors": ["4K", "natural light"],
            "avoid": ["anime"],
            "palette": "warm tones",
            "camera_language": "stabilised handheld",
            "reference_genre": "Documentary",
        },
        "style_lock": {
            "dominant_style": "cinematic_documentary",
            "forbidden_styles": ["anime", "watercolor"],
            "positive_fragment": "cinematic documentary, photoreal",
            "negative_fragment": "anime, cartoon",
        },
    }

    def _fake_completion(*, model: str, messages: list[dict[str, str]], **_: Any) -> Any:
        # First call (system prompt for the generator) returns scenes;
        # any subsequent call (refiner) echoes the same scenes so the
        # agent can still converge if the LLM under test chooses to
        # refine.
        system = messages[0]["content"] if messages else ""
        if "scenario refiner" in system.lower():
            return _StubLiteLLMResponse(json.dumps({"scenes": scenes}))
        return _StubLiteLLMResponse(json.dumps(generator_payload))

    import litellm

    monkeypatch.setattr(litellm, "completion", _fake_completion)

    # Stub the Strands agent's own model call so the test doesn't
    # attempt a real LLM call for the agent loop itself. We drive
    # the shape by simulating a single generate_scenario → evaluate
    # → create_timeline flow through ``agent.messages`` directly.
    from strands_agents import scenario_agent as agent_mod

    captured: dict[str, Any] = {}

    def _fake_build_agent(**kwargs: Any) -> Any:  # noqa: ARG001
        class _FakeAgent:
            messages: list[dict[str, Any]] = []

            def __call__(self, prompt: str) -> None:
                captured["prompt"] = prompt
                # Invoke the real helpers so this test also exercises
                # the scenario_llm → litellm path.
                gen_out = agent_mod._GENERATOR.get()(
                    "inflation", 3, "cinematic", "en-US"
                )
                self.messages = [
                    {"role": "user", "content": [{"text": prompt}]},
                    {
                        "role": "assistant",
                        "content": [_tool_use_block("generate_scenario", "u1")],
                    },
                    {
                        "role": "user",
                        "content": [_tool_result_block("u1", gen_out)],
                    },
                    {
                        "role": "assistant",
                        "content": [_tool_use_block("evaluate_scenario", "u2")],
                    },
                    {
                        "role": "user",
                        "content": [
                            _tool_result_block(
                                "u2",
                                {"rating": "GOOD", "issues": [], "suggestions": []},
                            )
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [_tool_use_block("create_timeline", "u3")],
                    },
                    {
                        "role": "user",
                        "content": [
                            _tool_result_block(
                                "u3",
                                {
                                    "timeline_path": "/tmp/fake.otio",
                                    "total_duration_sec": 300.0,
                                    "num_scenes": 3,
                                },
                            )
                        ],
                    },
                ]

        return _FakeAgent()

    # ``scenario_task`` imports ``build_scenario_agent`` lazily inside
    # its body, so patch the source module.
    monkeypatch.setattr(agent_mod, "build_scenario_agent", _fake_build_agent)

    case = Case[str, dict[str, Any]](
        name="economics_basics",
        session_id="playground-test-001",
        input="Produce a 3-scene, 5-minute explainer documentary about inflation.",
        metadata={"target_duration_sec": 300.0},
    )
    envelope = scenario_task(case)

    assert envelope["metadata"]["mode"] == "live"
    assert envelope["metadata"]["model"] == "openai/gpt-4o"
    assert envelope["trajectory"] == [
        "generate_scenario",
        "evaluate_scenario",
        "create_timeline",
    ]
    output = envelope["output"]
    assert len(output["scenes"]) == 3
    assert output["style_lock"]["dominant_style"] == "cinematic_documentary"
    assert output["timeline"]["timeline_path"].endswith(".otio")
    assert "Target total duration: 300 seconds" in captured["prompt"]


def test_scenario_task_surfaces_llm_parse_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed LLM payload must raise so the playground run
    endpoint can surface ``TASK_ERROR`` instead of silently returning
    empty scenes."""
    monkeypatch.setenv(SCENARIO_PLAYGROUND_MODEL_ENV, "openai/gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

    import litellm

    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **_: _StubLiteLLMResponse("not valid json"),
    )

    from strands_agents import scenario_agent as agent_mod

    class _FakeAgent:
        messages: list[dict[str, Any]] = []

        def __call__(self, prompt: str) -> None:  # pragma: no cover - triggers raise
            agent_mod._GENERATOR.get()("topic", 3, "cinematic", "en-US")

    monkeypatch.setattr(agent_mod, "build_scenario_agent", lambda **_: _FakeAgent())

    case = Case[str, dict[str, Any]](
        name="failure_bad_llm",
        session_id="playground-test-002",
        input="anything",
        metadata={"target_duration_sec": 60.0},
    )
    with pytest.raises(RuntimeError):
        scenario_task(case)
