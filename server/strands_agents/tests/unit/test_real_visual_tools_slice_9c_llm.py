"""Unit tests for the real-LLM visual tool overlay (slice 9c-LLM-visual).

Mirrors the test shape of
``tests/unit/test_real_scenario_tools_slice_9c_llm.py`` and covers:

* ``_resolve_model_id`` precedence: explicit arg > ``VISUAL_LLM_MODEL_ID``
  env > ``STRANDS_MODEL`` env > ``None``. Empty / whitespace-only
  strings are treated as unset.
* ``build_real_visual_tools`` is a no-op (returns ``{}``) when no
  model id resolves; returns ``{"propose_visual_concept": <tool>}``
  otherwise.
* ``apply_real_visual_overrides`` is purely additive when the
  override name is absent from the base list (overlay is appended);
  preserves order; replaces by ``.name`` when a same-named base tool
  exists.
* The built ``propose_visual_concept`` tool delegates to
  :func:`visual_llm.make_concept_proposer` and surfaces the LLM's
  prompt + concept dict in its envelope.
* The orchestrator builder (:func:`build_documentary_orchestrator`)
  appends ``propose_visual_concept`` when ``model`` is a string id
  and skips it when ``model`` is ``None`` / a non-string.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from strands_agents import _placeholders, _real_visual_tools
from strands_agents._real_visual_tools import (
    _resolve_model_id,
    apply_real_visual_overrides,
    build_real_visual_tools,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear visual-related env vars so each test starts from a known state."""
    for var in ("STRANDS_MODEL", "VISUAL_LLM_MODEL_ID", "SCENARIO_LLM_MODEL_ID"):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# Model id resolution
# ---------------------------------------------------------------------------


class TestResolveModelId:
    def test_explicit_arg_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRANDS_MODEL", "openai/gpt-4o")
        monkeypatch.setenv("VISUAL_LLM_MODEL_ID", "bedrock/anthropic.claude-3-5-sonnet")

        assert _resolve_model_id("explicit/model") == "explicit/model"

    def test_visual_env_wins_over_strands_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STRANDS_MODEL", "openai/gpt-4o")
        monkeypatch.setenv("VISUAL_LLM_MODEL_ID", "bedrock/anthropic.claude-3-5-sonnet")

        assert _resolve_model_id(None) == "bedrock/anthropic.claude-3-5-sonnet"

    def test_falls_back_to_strands_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRANDS_MODEL", "openai/gpt-4o")

        assert _resolve_model_id(None) == "openai/gpt-4o"

    def test_returns_none_when_unset(self) -> None:
        assert _resolve_model_id(None) is None

    def test_empty_explicit_arg_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STRANDS_MODEL", "openai/gpt-4o")

        assert _resolve_model_id("") == "openai/gpt-4o"
        assert _resolve_model_id("   ") == "openai/gpt-4o"

    def test_whitespace_env_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VISUAL_LLM_MODEL_ID", "   ")
        monkeypatch.setenv("STRANDS_MODEL", "openai/gpt-4o")

        assert _resolve_model_id(None) == "openai/gpt-4o"

    def test_strips_surrounding_whitespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VISUAL_LLM_MODEL_ID", "  openai/gpt-4o  ")

        assert _resolve_model_id(None) == "openai/gpt-4o"


# ---------------------------------------------------------------------------
# Tool builder gate logic
# ---------------------------------------------------------------------------


class TestBuildRealVisualTools:
    def test_no_op_when_unset(self) -> None:
        assert build_real_visual_tools() == {}

    def test_no_op_when_explicit_none_and_env_unset(self) -> None:
        assert build_real_visual_tools(model_id=None) == {}

    def test_overlay_when_explicit_model_id(self) -> None:
        tools = build_real_visual_tools(model_id="openai/gpt-4o")

        assert set(tools.keys()) == {"propose_visual_concept"}
        assert tools["propose_visual_concept"].name == "propose_visual_concept"

    def test_overlay_when_visual_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VISUAL_LLM_MODEL_ID", "openai/gpt-4o")

        tools = build_real_visual_tools()

        assert set(tools.keys()) == {"propose_visual_concept"}

    def test_overlay_when_strands_env_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STRANDS_MODEL", "openai/gpt-4o")

        tools = build_real_visual_tools()

        assert set(tools.keys()) == {"propose_visual_concept"}

    def test_returns_empty_when_visual_llm_import_fails(self) -> None:
        """Graceful degradation: import error returns ``{}`` not raises.

        Mirrors :func:`_real_scenario_tools.build_real_scenario_tools`'s
        defensive try/except so CI stays hermetic when the LLM stack
        is unavailable (no boto3 creds, no openai key, etc.) and the
        orchestrator transparently falls back to the placeholder
        visual concept builder.
        """
        with patch(
            "strands_agents.visual_llm.make_concept_proposer",
            side_effect=ImportError("simulated litellm import failure"),
        ):
            tools = build_real_visual_tools(model_id="openai/gpt-4o")

        assert tools == {}

    def test_visual_llm_not_imported_at_module_level(self) -> None:
        """``visual_llm`` must not be a top-level attr of the overlay.

        A top-level ``from strands_agents import visual_llm`` would
        crash module import the moment the LLM stack is unavailable,
        which would break the entire orchestrator boot sequence —
        not just the LLM overlay.
        """
        assert not hasattr(_real_visual_tools, "visual_llm"), (
            "visual_llm must be imported lazily inside _build_propose_concept_tool, "
            "not at module level"
        )


# ---------------------------------------------------------------------------
# Overlay application
# ---------------------------------------------------------------------------


class TestApplyRealVisualOverrides:
    def test_no_op_when_overrides_empty(self) -> None:
        base = [
            _placeholders.generate_scenario,
            _placeholders.launch_visual_production,
        ]
        result = apply_real_visual_overrides(base, {})

        assert result == base
        # New list, not the same reference.
        assert result is not base

    def test_appends_when_no_name_collision(self) -> None:
        overrides = build_real_visual_tools(model_id="openai/gpt-4o")
        base = [
            _placeholders.generate_scenario,
            _placeholders.launch_visual_production,
        ]

        result = apply_real_visual_overrides(base, overrides)

        names = [getattr(t, "name", None) for t in result]
        assert names == [
            "generate_scenario",
            "launch_visual_production",
            "propose_visual_concept",
        ]
        # Base tools pass through unchanged.
        assert result[0] is _placeholders.generate_scenario
        assert result[1] is _placeholders.launch_visual_production
        # Overlay tool is the LLM-backed one.
        assert result[2] is overrides["propose_visual_concept"]

    def test_replaces_when_name_matches(self) -> None:
        overrides = build_real_visual_tools(model_id="openai/gpt-4o")
        # Synthesize a fake base tool sharing the overlay's name.
        fake_existing = MagicMock()
        fake_existing.name = "propose_visual_concept"
        base = [
            _placeholders.generate_scenario,
            fake_existing,
            _placeholders.launch_visual_production,
        ]

        result = apply_real_visual_overrides(base, overrides)

        names = [getattr(t, "name", None) for t in result]
        assert names == [
            "generate_scenario",
            "propose_visual_concept",
            "launch_visual_production",
        ]
        # The overlay swapped in for the same-named base tool, not appended.
        assert result[1] is overrides["propose_visual_concept"]
        assert fake_existing not in result

    def test_preserves_order(self) -> None:
        overrides = build_real_visual_tools(model_id="openai/gpt-4o")
        base = [
            _placeholders.launch_assembly,
            _placeholders.refine_scenario,
            _placeholders.evaluate_scenario,
        ]

        result = apply_real_visual_overrides(base, overrides)

        names = [getattr(t, "name", None) for t in result]
        assert names == [
            "launch_assembly",
            "refine_scenario",
            "evaluate_scenario",
            "propose_visual_concept",
        ]


# ---------------------------------------------------------------------------
# propose_visual_concept tool delegation
# ---------------------------------------------------------------------------


class TestProposeVisualConceptTool:
    """Verify the built tool delegates to visual_llm.make_concept_proposer."""

    def test_delegates_to_proposer(self) -> None:
        captured: dict[str, Any] = {}

        def fake_proposer(
            phrase: dict[str, Any],
            style_lock: dict[str, Any],
            visual_style: dict[str, Any],
        ) -> dict[str, Any]:
            captured["phrase"] = phrase
            captured["style_lock"] = style_lock
            captured["visual_style"] = visual_style
            return {
                "shot_type": "establishing",
                "camera_movement": "dolly_in",
                "prompt": "A wide aerial shot of New York at golden hour.",
                "negative_prompt": "blurry, watermark",
                "duration_sec": 5.0,
                "ltx_params": {
                    "resolution": [1280, 720],
                    "seed": None,
                    "steps": 30,
                },
            }

        with patch(
            "strands_agents.visual_llm.make_concept_proposer",
            return_value=fake_proposer,
        ) as mocked_factory:
            tools = build_real_visual_tools(model_id="openai/gpt-4o")
            mocked_factory.assert_called_once_with(model_id="openai/gpt-4o")
            tool = tools["propose_visual_concept"]

            phrase = {
                "phrase_id": "p1",
                "phrase_type": "narrative",
                "text": "A bustling Wall Street",
                "time_span": [0.0, 5.0],
            }
            style_lock = {"dominant_style": "documentary"}
            visual_style = {"palette": "muted"}

            result = tool.invoke(
                {
                    "scene_id": "scene_001",
                    "phrase": phrase,
                    "style_lock": style_lock,
                    "visual_style": visual_style,
                }
            )

        # LangChain's tool.invoke pickles its arguments; identity is lost,
        # so compare by equality.
        assert captured["phrase"] == phrase
        assert captured["style_lock"] == style_lock
        assert captured["visual_style"] == visual_style
        assert result["scene_id"] == "scene_001"
        assert result["prompt"] == "A wide aerial shot of New York at golden hour."
        assert result["visual_concept"]["shot_type"] == "establishing"
        assert result["visual_concept"]["camera_movement"] == "dolly_in"

    def test_defaults_style_lock_and_visual_style_to_empty_dict(
        self,
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_proposer(
            phrase: dict[str, Any],
            style_lock: dict[str, Any],
            visual_style: dict[str, Any],
        ) -> dict[str, Any]:
            captured["style_lock"] = style_lock
            captured["visual_style"] = visual_style
            return {
                "shot_type": "wide",
                "camera_movement": "locked",
                "prompt": "x",
                "negative_prompt": "",
                "duration_sec": 3.0,
                "ltx_params": {},
            }

        with patch(
            "strands_agents.visual_llm.make_concept_proposer",
            return_value=fake_proposer,
        ):
            tools = build_real_visual_tools(model_id="openai/gpt-4o")
            tool = tools["propose_visual_concept"]
            tool.invoke(
                {
                    "scene_id": "scene_001",
                    "phrase": {"phrase_id": "p1"},
                    "style_lock": None,
                    "visual_style": None,
                }
            )

        assert captured["style_lock"] == {}
        assert captured["visual_style"] == {}

    def test_raises_on_empty_prompt(self) -> None:
        def fake_proposer(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "shot_type": "wide",
                "camera_movement": "locked",
                "prompt": "   ",
                "negative_prompt": "",
                "duration_sec": 3.0,
                "ltx_params": {},
            }

        with patch(
            "strands_agents.visual_llm.make_concept_proposer",
            return_value=fake_proposer,
        ):
            tools = build_real_visual_tools(model_id="openai/gpt-4o")
            tool = tools["propose_visual_concept"]
            # LangChain's @tool wraps exceptions in a ToolException; bypass
            # the wrapper by calling the underlying function directly.
            with pytest.raises(RuntimeError, match="empty prompt"):
                tool.func(
                    scene_id="scene_001",
                    phrase={"phrase_id": "p1"},
                    style_lock={},
                    visual_style={},
                )

    def test_does_not_call_litellm_when_proposer_mocked(self) -> None:
        """The tool delegates entirely to the proposer factory output."""

        def fake_proposer(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "shot_type": "wide",
                "camera_movement": "locked",
                "prompt": "ok",
                "negative_prompt": "",
                "duration_sec": 3.0,
                "ltx_params": {},
            }

        with (
            patch(
                "strands_agents.visual_llm.make_concept_proposer",
                return_value=fake_proposer,
            ),
            patch(
                "litellm.completion",
                side_effect=RuntimeError("litellm.completion must not be called"),
            ),
        ):
            tools = build_real_visual_tools(model_id="openai/gpt-4o")
            tool = tools["propose_visual_concept"]
            result = tool.invoke(
                {
                    "scene_id": "scene_001",
                    "phrase": {"phrase_id": "p1"},
                    "style_lock": {},
                    "visual_style": {},
                }
            )

        assert result["prompt"] == "ok"


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


class TestOrchestratorIntegration:
    """Verify build_documentary_orchestrator wires the visual overlay."""

    def _names(self, agent: Any) -> list[str]:
        # The deepagent stores its tools on a state-graph attribute. Cheaper
        # path: enumerate the kwargs passed to the underlying builder via a
        # spy. Done in each test.
        raise NotImplementedError

    def test_no_overlay_without_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STRANDS_MODEL", raising=False)
        monkeypatch.delenv("VISUAL_LLM_MODEL_ID", raising=False)
        monkeypatch.delenv("SCENARIO_LLM_MODEL_ID", raising=False)
        from strands_agents import pipeline

        captured_tools: list[Any] = []

        def fake_build_orchestrator(*args: Any, **kwargs: Any) -> Any:
            captured_tools.extend(kwargs["tools"])
            return MagicMock()

        with patch.object(
            pipeline, "build_orchestrator", side_effect=fake_build_orchestrator
        ):
            pipeline.build_documentary_orchestrator(run_dir=tmp_path)

        names = [getattr(t, "name", None) for t in captured_tools]
        assert "propose_visual_concept" not in names

    def test_overlay_appended_when_model_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STRANDS_MODEL", raising=False)
        monkeypatch.delenv("VISUAL_LLM_MODEL_ID", raising=False)
        monkeypatch.delenv("SCENARIO_LLM_MODEL_ID", raising=False)
        from strands_agents import pipeline

        captured_tools: list[Any] = []

        def fake_build_orchestrator(*args: Any, **kwargs: Any) -> Any:
            captured_tools.extend(kwargs["tools"])
            return MagicMock()

        # Patch the LLM factories so no network call is attempted.
        with (
            patch.object(
                pipeline, "build_orchestrator", side_effect=fake_build_orchestrator
            ),
            patch(
                "strands_agents.scenario_llm.make_generator",
                return_value=lambda *a, **k: {"scenes": []},
            ),
            patch(
                "strands_agents.scenario_llm.make_refiner",
                return_value=lambda *a, **k: {"scenes": []},
            ),
            patch(
                "strands_agents.visual_llm.make_concept_proposer",
                return_value=lambda *a, **k: {
                    "shot_type": "wide",
                    "camera_movement": "locked",
                    "prompt": "x",
                    "negative_prompt": "",
                    "duration_sec": 3.0,
                    "ltx_params": {},
                },
            ),
        ):
            pipeline.build_documentary_orchestrator(
                run_dir=tmp_path,
                model="openai/gpt-4o",
            )

        names = [getattr(t, "name", None) for t in captured_tools]
        assert "propose_visual_concept" in names

    def test_no_overlay_when_model_is_non_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A BaseChatModel object (not a string) should not be treated as a model id."""
        monkeypatch.delenv("STRANDS_MODEL", raising=False)
        monkeypatch.delenv("VISUAL_LLM_MODEL_ID", raising=False)
        monkeypatch.delenv("SCENARIO_LLM_MODEL_ID", raising=False)
        from strands_agents import pipeline

        captured_tools: list[Any] = []

        def fake_build_orchestrator(*args: Any, **kwargs: Any) -> Any:
            captured_tools.extend(kwargs["tools"])
            return MagicMock()

        fake_model_obj = MagicMock()
        # Explicitly NOT a string.
        assert not isinstance(fake_model_obj, str)

        with patch.object(
            pipeline, "build_orchestrator", side_effect=fake_build_orchestrator
        ):
            pipeline.build_documentary_orchestrator(
                run_dir=tmp_path,
                model=fake_model_obj,
            )

        names = [getattr(t, "name", None) for t in captured_tools]
        assert "propose_visual_concept" not in names
