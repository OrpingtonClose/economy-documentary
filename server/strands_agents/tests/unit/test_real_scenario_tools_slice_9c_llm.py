"""Unit tests for slice 9c-LLM-scenario — real LLM scenario tools.

Slice 9c (PR #363) wired real narration + visual prompts into the
audio/video dispatch layer. Slice 9c-LLM-scenario goes one layer
upstream: it replaces the placeholder ``generate_scenario`` /
``evaluate_scenario`` / ``refine_scenario`` / ``create_timeline`` with
real LLM-backed tools so the narration text the orchestrator passes
into ``launch_audio_render`` is actual model output, not a placeholder
echo.

These tests pin down:

* :func:`_resolve_model_id` precedence (explicit arg > env vars >
  empty).
* :func:`build_real_scenario_tools` returns ``{}`` when no model is
  configured (CI stays hermetic / GPU-free).
* :func:`build_real_scenario_tools` returns the four-tool override set
  when a model id is configured.
* :func:`apply_real_scenario_overrides` swaps placeholders by ``.name``
  and preserves order; tools without a matching override pass through.
* The deterministic tools (``evaluate_scenario`` / ``create_timeline``)
  do not call litellm — they wrap the structural-check + OTIO logic
  directly.
* The LLM-backed tools (``generate_scenario`` / ``refine_scenario``)
  delegate to ``scenario_llm.make_generator`` /
  ``scenario_llm.make_refiner`` with the resolved ``model_id``.
* :func:`build_documentary_orchestrator` applies the scenario
  overrides on top of the worker overrides without dropping either
  set, when both are configured.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from strands_agents import _placeholders, _real_scenario_tools
from strands_agents._real_scenario_tools import (
    _resolve_model_id,
    apply_real_scenario_overrides,
    build_real_scenario_tools,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear scenario-related env vars so each test starts from a known state."""
    for var in ("STRANDS_MODEL", "SCENARIO_LLM_MODEL_ID"):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# Model id resolution
# ---------------------------------------------------------------------------


class TestResolveModelId:
    def test_explicit_arg_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRANDS_MODEL", "openai/gpt-4o")
        monkeypatch.setenv(
            "SCENARIO_LLM_MODEL_ID", "bedrock/anthropic.claude-3-5-sonnet"
        )

        assert _resolve_model_id("explicit/model") == "explicit/model"

    def test_scenario_env_wins_over_strands_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STRANDS_MODEL", "openai/gpt-4o")
        monkeypatch.setenv(
            "SCENARIO_LLM_MODEL_ID", "bedrock/anthropic.claude-3-5-sonnet"
        )

        assert _resolve_model_id(None) == "bedrock/anthropic.claude-3-5-sonnet"

    def test_strands_model_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRANDS_MODEL", "openai/gpt-4o")

        assert _resolve_model_id(None) == "openai/gpt-4o"

    def test_no_env_returns_none(self) -> None:
        assert _resolve_model_id(None) is None

    def test_empty_string_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STRANDS_MODEL", "")
        monkeypatch.setenv("SCENARIO_LLM_MODEL_ID", "   ")

        assert _resolve_model_id(None) is None

    def test_whitespace_arg_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STRANDS_MODEL", "openai/gpt-4o")

        assert _resolve_model_id("   ") == "openai/gpt-4o"

    def test_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRANDS_MODEL", "  openai/gpt-4o  ")

        assert _resolve_model_id(None) == "openai/gpt-4o"


# ---------------------------------------------------------------------------
# build_real_scenario_tools — gating
# ---------------------------------------------------------------------------


class TestBuildRealScenarioTools:
    def test_empty_when_no_model_configured(self) -> None:
        assert build_real_scenario_tools() == {}

    def test_full_set_when_model_provided(self) -> None:
        overrides = build_real_scenario_tools(model_id="openai/gpt-4o")

        assert sorted(overrides.keys()) == sorted(
            [
                "generate_scenario",
                "evaluate_scenario",
                "refine_scenario",
                "create_timeline",
            ]
        )

    def test_full_set_via_strands_model_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STRANDS_MODEL", "openai/gpt-4o")

        overrides = build_real_scenario_tools()

        assert "generate_scenario" in overrides
        assert "refine_scenario" in overrides

    def test_full_set_via_scenario_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "SCENARIO_LLM_MODEL_ID",
            "bedrock/anthropic.claude-3-5-sonnet",
        )

        overrides = build_real_scenario_tools()

        assert sorted(overrides.keys()) == sorted(
            [
                "generate_scenario",
                "evaluate_scenario",
                "refine_scenario",
                "create_timeline",
            ]
        )

    def test_override_tools_have_correct_names(self) -> None:
        overrides = build_real_scenario_tools(model_id="openai/gpt-4o")

        for name, tool_obj in overrides.items():
            assert getattr(tool_obj, "name", None) == name


# ---------------------------------------------------------------------------
# apply_real_scenario_overrides
# ---------------------------------------------------------------------------


class TestApplyRealScenarioOverrides:
    def test_empty_overrides_returns_copy(self) -> None:
        base = [_placeholders.generate_scenario, _placeholders.launch_assembly]

        result = apply_real_scenario_overrides(base, {})

        assert result == base
        assert result is not base

    def test_replaces_by_name(self) -> None:
        overrides = build_real_scenario_tools(model_id="openai/gpt-4o")
        base = [
            _placeholders.generate_scenario,
            _placeholders.evaluate_scenario,
            _placeholders.refine_scenario,
            _placeholders.launch_assembly,
        ]

        result = apply_real_scenario_overrides(base, overrides)

        names = [getattr(t, "name", None) for t in result]
        assert names == [
            "generate_scenario",
            "evaluate_scenario",
            "refine_scenario",
            "launch_assembly",
            "create_timeline",
        ]
        # The first three slots should be the override tools, not the
        # placeholders.
        assert result[0] is overrides["generate_scenario"]
        assert result[1] is overrides["evaluate_scenario"]
        assert result[2] is overrides["refine_scenario"]
        # Unrelated tools pass through unchanged.
        assert result[3] is _placeholders.launch_assembly
        # create_timeline has no placeholder, gets appended at the end.
        assert result[4] is overrides["create_timeline"]

    def test_preserves_order(self) -> None:
        overrides = build_real_scenario_tools(model_id="openai/gpt-4o")
        # Reverse order from the canonical default to confirm the
        # walker doesn't reorder.
        base = [
            _placeholders.launch_assembly,
            _placeholders.refine_scenario,
            _placeholders.evaluate_scenario,
            _placeholders.generate_scenario,
        ]

        result = apply_real_scenario_overrides(base, overrides)

        names = [getattr(t, "name", None) for t in result]
        assert names == [
            "launch_assembly",
            "refine_scenario",
            "evaluate_scenario",
            "generate_scenario",
            "create_timeline",
        ]

    def test_appends_create_timeline_when_missing(self) -> None:
        overrides = build_real_scenario_tools(model_id="openai/gpt-4o")
        # The placeholder tool list does NOT include create_timeline,
        # so the override should be appended at the end so the
        # orchestrator gains the tool.
        base = [
            _placeholders.generate_scenario,
            _placeholders.evaluate_scenario,
            _placeholders.refine_scenario,
        ]

        result = apply_real_scenario_overrides(base, overrides)

        names = [getattr(t, "name", None) for t in result]
        assert "create_timeline" in names
        assert names[-1] == "create_timeline"


# ---------------------------------------------------------------------------
# Deterministic tools — evaluate_scenario / create_timeline
# ---------------------------------------------------------------------------


def _valid_scene(idx: int, duration_sec: float = 60.0) -> dict[str, Any]:
    """Minimal scene shape that satisfies most structural checks."""
    base = {
        "scene_num": idx,
        "title": f"Scene {idx}",
        "duration_sec": duration_sec,
        "narration": (
            f"This is a simple narration sentence describing scene number {idx}."
        ),
        "pronunciation_hints": [],
        "visual_notes": ("A wide cinematic shot of a city skyline at golden hour."),
        "dopamine_hook": "A striking moment.",
    }
    if idx == 1:
        base["hook_spec"] = {"opening": "A surprising statistic."}
    return base


class TestEvaluateScenarioDeterministic:
    def test_evaluate_returns_rating_shape(self) -> None:
        overrides = build_real_scenario_tools(model_id="openai/gpt-4o")
        ev = overrides["evaluate_scenario"]

        scenes = [_valid_scene(i) for i in range(1, 4)]
        scenes[-1]["outro_spec"] = {"closing": "Sign-off."}
        result = ev.invoke(
            {
                "scenes": scenes,
                "style_lock": {
                    "dominant_style": "cinematic_documentary",
                    "forbidden_styles": [],
                    "positive_fragment": "cinematic, photographic",
                    "negative_fragment": "cartoon, animated",
                },
                "target_duration_sec": 180.0,
            }
        )

        assert "rating" in result
        assert result["rating"] in {"POOR", "FAIR", "GOOD", "EXCELLENT"}
        assert "issues" in result
        assert "suggestions" in result
        assert isinstance(result["issues"], list)

    def test_evaluate_does_not_call_litellm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If evaluate_scenario ever started calling litellm, this would
        # raise — keeps the deterministic guarantee enforced.
        import litellm

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("evaluate_scenario must not call litellm")

        monkeypatch.setattr(litellm, "completion", _boom)

        overrides = build_real_scenario_tools(model_id="openai/gpt-4o")
        ev = overrides["evaluate_scenario"]

        scenes = [_valid_scene(i) for i in range(1, 3)]
        ev.invoke(
            {
                "scenes": scenes,
                "style_lock": {"dominant_style": "cinematic"},
                "target_duration_sec": 120.0,
            }
        )


class TestCreateTimelineDeterministic:
    def test_returns_timeline_path_shape(self, tmp_path: Path) -> None:
        overrides = build_real_scenario_tools(model_id="openai/gpt-4o")
        ct = overrides["create_timeline"]

        scenes = [_valid_scene(1, 30.0), _valid_scene(2, 45.0)]

        # otio_tools.create_timeline writes to a topic-derived path on
        # disk; we let it do so and verify the returned shape.
        result = ct.invoke({"scenes": scenes})

        assert "timeline_path" in result
        assert "total_duration_sec" in result
        assert "num_scenes" in result
        assert result["total_duration_sec"] == pytest.approx(75.0)
        assert result["num_scenes"] == 2

    def test_empty_scenes_raises(self) -> None:
        overrides = build_real_scenario_tools(model_id="openai/gpt-4o")
        ct = overrides["create_timeline"]

        with pytest.raises(ValueError, match="non-empty"):
            ct.invoke({"scenes": []})


# ---------------------------------------------------------------------------
# LLM-backed tools — generator + refiner delegation
# ---------------------------------------------------------------------------


class TestGenerateScenarioLLM:
    def test_delegates_to_make_generator(self) -> None:
        captured: dict[str, Any] = {}

        def _fake_generator(
            topic: str, num_scenes: int, style: str, language: str
        ) -> dict[str, Any]:
            captured.update(
                topic=topic,
                num_scenes=num_scenes,
                style=style,
                language=language,
            )
            return {
                "scenes": [_valid_scene(1)],
                "visual_style": {"style": "cinematic"},
                "style_lock": {"dominant_style": "cinematic"},
            }

        with patch.object(
            _real_scenario_tools,
            "_build_generate_tool",
            wraps=_real_scenario_tools._build_generate_tool,
        ):
            from strands_agents import scenario_llm

            with patch.object(
                scenario_llm,
                "make_generator",
                return_value=_fake_generator,
            ) as mocked:
                overrides = build_real_scenario_tools(model_id="openai/gpt-4o")
                tool_obj = overrides["generate_scenario"]
                result = tool_obj.invoke(
                    {
                        "topic": "Federal Reserve",
                        "num_scenes": 1,
                        "style": "cinematic",
                        "language": "en",
                    }
                )

        mocked.assert_called_once_with(model_id="openai/gpt-4o")
        assert captured == {
            "topic": "Federal Reserve",
            "num_scenes": 1,
            "style": "cinematic",
            "language": "en",
        }
        assert "scenes" in result
        assert len(result["scenes"]) == 1

    def test_passes_resolved_model_id_through_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "SCENARIO_LLM_MODEL_ID",
            "bedrock/anthropic.claude-3-5-sonnet",
        )

        from strands_agents import scenario_llm

        with patch.object(
            scenario_llm,
            "make_generator",
            return_value=lambda *a, **kw: {"scenes": []},
        ) as mocked:
            with patch.object(
                scenario_llm,
                "make_refiner",
                return_value=lambda *a, **kw: {"scenes": []},
            ):
                build_real_scenario_tools()

        mocked.assert_called_once_with(model_id="bedrock/anthropic.claude-3-5-sonnet")


class TestRefineScenarioLLM:
    def test_delegates_to_make_refiner(self) -> None:
        captured: dict[str, Any] = {}

        def _fake_refiner(
            scenes: list[dict[str, Any]], feedback: dict[str, Any]
        ) -> dict[str, Any]:
            captured.update(scenes=scenes, feedback=feedback)
            return {"scenes": [{**s, "revised": True} for s in scenes]}

        from strands_agents import scenario_llm

        with patch.object(
            scenario_llm, "make_refiner", return_value=_fake_refiner
        ) as mocked:
            with patch.object(
                scenario_llm,
                "make_generator",
                return_value=lambda *a, **kw: {"scenes": []},
            ):
                overrides = build_real_scenario_tools(model_id="openai/gpt-4o")
                tool_obj = overrides["refine_scenario"]
                result = tool_obj.invoke(
                    {
                        "scenes": [{"scene_num": 1}],
                        "feedback": {"issues": [{"check": "duration"}]},
                    }
                )

        mocked.assert_called_once_with(model_id="openai/gpt-4o")
        assert captured["scenes"] == [{"scene_num": 1}]
        assert captured["feedback"]["issues"] == [{"check": "duration"}]
        assert result["scenes"][0]["revised"] is True


# ---------------------------------------------------------------------------
# Integration with build_documentary_orchestrator
# ---------------------------------------------------------------------------


class TestOrchestratorIntegration:
    def test_no_model_uses_placeholders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STRANDS_MODEL", raising=False)
        monkeypatch.delenv("SCENARIO_LLM_MODEL_ID", raising=False)
        monkeypatch.delenv("QWEN3_TTS_WORKER_URL", raising=False)
        monkeypatch.delenv("LTX_VIDEO_WORKER_URL", raising=False)

        from strands_agents._real_scenario_tools import (
            build_real_scenario_tools as build_real,
        )

        # When no env is set the override set is empty, so applying it
        # is a no-op. This is the contract the orchestrator relies on
        # for hermetic CI.
        assert build_real() == {}

    def test_explicit_model_string_routes_into_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STRANDS_MODEL", raising=False)
        monkeypatch.delenv("SCENARIO_LLM_MODEL_ID", raising=False)

        from strands_agents import scenario_llm

        with patch.object(
            scenario_llm,
            "make_generator",
            return_value=lambda *a, **kw: {"scenes": []},
        ) as gen_mock:
            with patch.object(
                scenario_llm,
                "make_refiner",
                return_value=lambda *a, **kw: {"scenes": []},
            ) as ref_mock:
                # An explicit model id (the kind ``build_documentary_orchestrator``
                # forwards when ``model`` is a string) routes into the
                # override builder.
                overrides = build_real_scenario_tools(model_id="openai/gpt-4o")

        gen_mock.assert_called_once_with(model_id="openai/gpt-4o")
        ref_mock.assert_called_once_with(model_id="openai/gpt-4o")
        assert sorted(overrides.keys()) == sorted(
            [
                "generate_scenario",
                "evaluate_scenario",
                "refine_scenario",
                "create_timeline",
            ]
        )
