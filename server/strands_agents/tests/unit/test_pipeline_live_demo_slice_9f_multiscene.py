"""Unit tests for slice 9f-multiscene-prod (scripted-demo loop).

The slice extends :func:`pipeline_live_demo._demo_chat_script` from a
single hardcoded ``scene_001`` to ``N`` scenes derived from
``target_duration_sec`` (or an explicit ``num_scenes`` override). The
batched ``launch_audio_render`` / ``launch_visual_production`` AIMessages
match the AGENTS.md "Timing stage" parallel-launch shape.

These tests assert:

* Scene-count resolver precedence + clamping.
* Per-scene fixture determinism (id, narration, visual concept, prompt).
* Script shape for ``num_scenes`` ∈ {1, 3, 6}: tool counts, batching,
  scene-id uniqueness, narration/prompt diversity.
* The orchestrator system prompt (``ORCHESTRATOR_PROMPT``) carries the
  multi-scene anti-drift guidance so a future LLM rewrite can't quietly
  collapse the loop.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage

from strands_agents import pipeline as pipeline_mod
from strands_agents.playground import pipeline_live_demo as demo


def _tool_calls(msg: AIMessage) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", msg.tool_calls)


def _names(script: list[AIMessage]) -> list[str]:
    """Flat list of tool-call names across the whole script.

    Batched AIMessages (``_ai_tool_calls_batch``) contribute multiple
    entries; the final ``_ai_final`` AIMessage contributes none.
    """
    flat: list[str] = []
    for msg in script:
        flat.extend(call["name"] for call in _tool_calls(msg))
    return flat


# ---------------------------------------------------------------------------
# _resolve_num_scenes
# ---------------------------------------------------------------------------


class TestResolveNumScenes:
    """``_resolve_num_scenes`` clamps to ``[1, 6]`` and derives from duration."""

    def test_explicit_override_wins_over_duration(self) -> None:
        assert demo._resolve_num_scenes(target_duration_sec=600, num_scenes=2) == 2

    def test_explicit_override_clamped_low(self) -> None:
        assert demo._resolve_num_scenes(target_duration_sec=60, num_scenes=0) == 1

    def test_explicit_override_clamped_high(self) -> None:
        assert demo._resolve_num_scenes(target_duration_sec=60, num_scenes=99) == 6

    def test_derives_one_scene_for_short_duration(self) -> None:
        # 12s / 12s-per-scene = 1 scene.
        assert demo._resolve_num_scenes(target_duration_sec=12, num_scenes=None) == 1

    def test_derives_three_scenes_for_thirty_seconds(self) -> None:
        # ceil(30 / 12) = 3.
        assert demo._resolve_num_scenes(target_duration_sec=30, num_scenes=None) == 3

    def test_derives_five_scenes_for_sixty_seconds(self) -> None:
        # ceil(60 / 12) = 5.
        assert demo._resolve_num_scenes(target_duration_sec=60, num_scenes=None) == 5

    def test_derives_six_scenes_for_seventy_two_seconds(self) -> None:
        # ceil(72 / 12) = 6.
        assert demo._resolve_num_scenes(target_duration_sec=72, num_scenes=None) == 6

    def test_clamps_to_six_for_long_duration(self) -> None:
        assert demo._resolve_num_scenes(target_duration_sec=600, num_scenes=None) == 6

    def test_zero_duration_falls_back_to_one(self) -> None:
        assert demo._resolve_num_scenes(target_duration_sec=0, num_scenes=None) == 1

    def test_negative_duration_falls_back_to_one(self) -> None:
        assert demo._resolve_num_scenes(target_duration_sec=-5, num_scenes=None) == 1


# ---------------------------------------------------------------------------
# _build_scene_payload
# ---------------------------------------------------------------------------


class TestBuildScenePayload:
    """Per-scene fixture is deterministic and includes all dispatch fields."""

    def test_scene_id_zero_padded(self) -> None:
        s1 = demo._build_scene_payload(1, "topic", 12.0, num_scenes=3)
        s12 = demo._build_scene_payload(12, "topic", 12.0, num_scenes=12)
        assert s1["scene_id"] == "scene_001"
        assert s12["scene_id"] == "scene_012"

    def test_required_keys_present(self) -> None:
        scene = demo._build_scene_payload(1, "The Federal Reserve", 12.0, num_scenes=1)
        for key in (
            "scene_id",
            "narration_text",
            "visual_concept",
            "visual_prompt",
            "duration_sec",
        ):
            assert key in scene, f"missing {key} in scene payload"

    def test_narration_text_includes_topic(self) -> None:
        scene = demo._build_scene_payload(1, "The Federal Reserve", 12.0, num_scenes=1)
        assert "The Federal Reserve" in scene["narration_text"]

    def test_visual_prompt_includes_topic(self) -> None:
        scene = demo._build_scene_payload(
            2, "Climate Tipping Points", 15.0, num_scenes=3
        )
        assert "Climate Tipping Points" in scene["visual_prompt"]

    def test_visual_concept_carries_style_lock_fields(self) -> None:
        scene = demo._build_scene_payload(3, "topic", 20.0, num_scenes=5)
        concept = scene["visual_concept"]
        for key in (
            "shot_count",
            "style",
            "shot_type",
            "camera_movement",
            "mood",
            "palette",
            "phrases",
        ):
            assert key in concept, f"missing {key} in visual_concept"

    def test_duration_passes_through(self) -> None:
        scene = demo._build_scene_payload(1, "topic", 17.5, num_scenes=1)
        assert scene["duration_sec"] == 17.5

    def test_distinct_scenes_get_distinct_narration(self) -> None:
        s1 = demo._build_scene_payload(1, "topic", 12.0, num_scenes=3)
        s2 = demo._build_scene_payload(2, "topic", 12.0, num_scenes=3)
        assert s1["narration_text"] != s2["narration_text"]

    def test_distinct_scenes_get_distinct_visual_prompts(self) -> None:
        s1 = demo._build_scene_payload(1, "topic", 12.0, num_scenes=3)
        s2 = demo._build_scene_payload(2, "topic", 12.0, num_scenes=3)
        assert s1["visual_prompt"] != s2["visual_prompt"]

    def test_first_scene_labelled_opening(self) -> None:
        scene = demo._build_scene_payload(1, "topic", 12.0, num_scenes=3)
        assert "opening" in scene["narration_text"]
        assert "opening" in scene["visual_prompt"]

    def test_last_scene_labelled_closing_for_multi_scene_runs(self) -> None:
        scene = demo._build_scene_payload(3, "topic", 12.0, num_scenes=3)
        assert "closing" in scene["narration_text"]
        assert "closing" in scene["visual_prompt"]

    def test_middle_scene_labelled_beat(self) -> None:
        scene = demo._build_scene_payload(2, "topic", 12.0, num_scenes=4)
        assert "beat 2" in scene["narration_text"]
        assert "beat 2" in scene["visual_prompt"]

    def test_single_scene_run_keeps_opening_label(self) -> None:
        scene = demo._build_scene_payload(1, "topic", 12.0, num_scenes=1)
        assert "opening" in scene["narration_text"]
        assert "closing" not in scene["narration_text"]

    def test_two_scene_run_labels_first_opening_and_second_closing(self) -> None:
        s1 = demo._build_scene_payload(1, "topic", 12.0, num_scenes=2)
        s2 = demo._build_scene_payload(2, "topic", 12.0, num_scenes=2)
        assert "opening" in s1["narration_text"]
        assert "closing" in s2["narration_text"]


# ---------------------------------------------------------------------------
# _demo_chat_script — single scene (regression)
# ---------------------------------------------------------------------------


class TestDemoChatScriptSingleScene:
    """The pre-9f single-scene shape stays intact for ``num_scenes=1``."""

    @pytest.fixture
    def script(self) -> list[AIMessage]:
        return demo._demo_chat_script(
            topic="The Federal Reserve",
            target_duration_sec=60,
            language="en",
            num_scenes=1,
        )

    def test_each_tool_called_once(self, script: list[AIMessage]) -> None:
        names = _names(script)
        for tool in (
            "generate_scenario",
            "evaluate_scenario",
            "launch_audio_render",
            "evaluate_timing",
            "content_analyst",
            "visual_concepter",
            "launch_visual_production",
            "launch_assembly",
            "launch_b2_sync",
        ):
            assert names.count(tool) == 1, (
                f"expected exactly one {tool}, got {names.count(tool)}"
            )

    def test_ends_with_final_message(self, script: list[AIMessage]) -> None:
        final = script[-1]
        assert final.tool_calls == []
        assert "Final master MP4" in str(final.content)

    def test_ordering_audio_before_timing(self, script: list[AIMessage]) -> None:
        names = _names(script)
        assert names.index("launch_audio_render") < names.index("evaluate_timing")

    def test_ordering_visual_concepter_before_production(
        self, script: list[AIMessage]
    ) -> None:
        names = _names(script)
        assert names.index("visual_concepter") < names.index("launch_visual_production")


# ---------------------------------------------------------------------------
# _demo_chat_script — multi-scene loop
# ---------------------------------------------------------------------------


class TestDemoChatScriptMultiScene:
    """Slice 9f: N scenes => N batched audio + N batched visual calls."""

    @pytest.mark.parametrize("n", [1, 2, 3, 5, 6])
    def test_audio_render_count_matches_num_scenes(self, n: int) -> None:
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=n
        )
        assert _names(script).count("launch_audio_render") == n

    @pytest.mark.parametrize("n", [1, 2, 3, 5, 6])
    def test_visual_production_count_matches_num_scenes(self, n: int) -> None:
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=n
        )
        assert _names(script).count("launch_visual_production") == n

    @pytest.mark.parametrize("n", [1, 2, 3, 5, 6])
    def test_visual_concepter_count_matches_num_scenes(self, n: int) -> None:
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=n
        )
        names = _names(script)
        assert names.count("content_analyst") == n
        assert names.count("visual_concepter") == n

    @pytest.mark.parametrize("n", [1, 2, 3, 5, 6])
    def test_singleton_calls_unchanged_by_n(self, n: int) -> None:
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=n
        )
        names = _names(script)
        for tool in (
            "generate_scenario",
            "evaluate_scenario",
            "evaluate_timing",
            "launch_assembly",
            "launch_b2_sync",
        ):
            assert names.count(tool) == 1, (
                f"{tool} fired {names.count(tool)} times for n={n}"
            )

    def test_audio_renders_batched_on_one_message(self) -> None:
        """All N ``launch_audio_render`` calls live on a single AIMessage.

        Matches AGENTS.md "Timing stage" — batch launches.
        """
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=4
        )
        audio_msgs = [
            msg
            for msg in script
            if any(call["name"] == "launch_audio_render" for call in _tool_calls(msg))
        ]
        assert len(audio_msgs) == 1, (
            "audio renders must batch onto one AIMessage (AGENTS.md timing stage)"
        )
        assert len(_tool_calls(audio_msgs[0])) == 4

    def test_visual_productions_batched_on_one_message(self) -> None:
        """All N ``launch_visual_production`` calls live on a single AIMessage."""
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=4
        )
        prod_msgs = [
            msg
            for msg in script
            if any(
                call["name"] == "launch_visual_production" for call in _tool_calls(msg)
            )
        ]
        assert len(prod_msgs) == 1
        assert len(_tool_calls(prod_msgs[0])) == 4

    def test_scene_ids_unique_across_audio_calls(self) -> None:
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=5
        )
        audio_calls: list[dict[str, Any]] = []
        for msg in script:
            for call in _tool_calls(msg):
                if call["name"] == "launch_audio_render":
                    audio_calls.append(call)
        ids = [call["args"]["scene_id"] for call in audio_calls]
        assert len(ids) == len(set(ids))
        assert ids == ["scene_001", "scene_002", "scene_003", "scene_004", "scene_005"]

    def test_scene_ids_unique_across_visual_calls(self) -> None:
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=5
        )
        visual_calls: list[dict[str, Any]] = []
        for msg in script:
            for call in _tool_calls(msg):
                if call["name"] == "launch_visual_production":
                    visual_calls.append(call)
        ids = [call["args"]["scene_id"] for call in visual_calls]
        assert ids == ["scene_001", "scene_002", "scene_003", "scene_004", "scene_005"]

    def test_audio_and_visual_scene_ids_align(self) -> None:
        """The same scene_id space is used by audio and visual dispatch."""
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=4
        )
        audio_ids: set[str] = set()
        visual_ids: set[str] = set()
        for msg in script:
            for call in _tool_calls(msg):
                if call["name"] == "launch_audio_render":
                    audio_ids.add(call["args"]["scene_id"])
                elif call["name"] == "launch_visual_production":
                    visual_ids.add(call["args"]["scene_id"])
        assert audio_ids == visual_ids

    def test_narration_varies_across_scenes(self) -> None:
        script = demo._demo_chat_script(
            topic="The Federal Reserve",
            target_duration_sec=60,
            language="en",
            num_scenes=3,
        )
        narrations: list[str] = []
        for msg in script:
            for call in _tool_calls(msg):
                if call["name"] == "launch_audio_render":
                    narrations.append(call["args"]["text"])
        assert len(narrations) == 3
        assert len(set(narrations)) == 3, "every scene must get unique narration"

    def test_visual_prompts_vary_across_scenes(self) -> None:
        script = demo._demo_chat_script(
            topic="The Federal Reserve",
            target_duration_sec=60,
            language="en",
            num_scenes=3,
        )
        prompts: list[str] = []
        for msg in script:
            for call in _tool_calls(msg):
                if call["name"] == "launch_visual_production":
                    prompts.append(call["args"]["prompt"])
        assert len(prompts) == 3
        assert len(set(prompts)) == 3, "every scene must get unique visual prompt"

    def test_audio_text_carries_topic(self) -> None:
        """Slice 9c invariant — narration text is real per-scene script.

        Slice 9f preserves it: every batched audio call must still carry
        a ``text`` arg containing the topic.
        """
        topic = "Climate Tipping Points"
        script = demo._demo_chat_script(
            topic=topic, target_duration_sec=60, language="en", num_scenes=3
        )
        audio_calls: list[dict[str, Any]] = []
        for msg in script:
            for call in _tool_calls(msg):
                if call["name"] == "launch_audio_render":
                    audio_calls.append(call)
        for call in audio_calls:
            text = call["args"]["text"]
            assert isinstance(text, str) and text.strip()
            assert topic in text, (
                f"narration must reference topic {topic!r}, got {text!r}"
            )

    def test_visual_prompt_carries_topic(self) -> None:
        """Slice 9c invariant — visual prompts are real LTX-aware strings."""
        topic = "Cryptocurrency Mining"
        script = demo._demo_chat_script(
            topic=topic, target_duration_sec=60, language="en", num_scenes=3
        )
        visual_calls: list[dict[str, Any]] = []
        for msg in script:
            for call in _tool_calls(msg):
                if call["name"] == "launch_visual_production":
                    visual_calls.append(call)
        for call in visual_calls:
            prompt = call["args"]["prompt"]
            assert isinstance(prompt, str) and prompt.strip()
            assert topic in prompt

    def test_scenario_envelope_carries_n_scenes(self) -> None:
        """``generate_scenario`` arg should encode the requested scene count."""
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=4
        )
        gen = next(
            call
            for msg in script
            for call in _tool_calls(msg)
            if call["name"] == "generate_scenario"
        )
        assert gen["args"]["num_scenes"] == 4

    def test_evaluate_scenario_carries_n_scenes(self) -> None:
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=4
        )
        eval_call = next(
            call
            for msg in script
            for call in _tool_calls(msg)
            if call["name"] == "evaluate_scenario"
        )
        assert len(eval_call["args"]["scenes"]) == 4
        ids = [s["id"] for s in eval_call["args"]["scenes"]]
        assert ids == ["scene_001", "scene_002", "scene_003", "scene_004"]

    def test_timeline_payload_carries_all_scene_ids(self) -> None:
        """``evaluate_timing`` + ``launch_assembly`` see every scene_id."""
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=3
        )
        timing_call = next(
            call
            for msg in script
            for call in _tool_calls(msg)
            if call["name"] == "evaluate_timing"
        )
        assert timing_call["args"]["timeline"]["scenes"] == [
            "scene_001",
            "scene_002",
            "scene_003",
        ]

    def test_evaluate_timing_fires_after_all_audio(self) -> None:
        """``evaluate_timing`` happens once, after the batched audio launch."""
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=4
        )
        names = _names(script)
        last_audio = max(i for i, n in enumerate(names) if n == "launch_audio_render")
        timing = names.index("evaluate_timing")
        assert timing > last_audio

    def test_assembly_fires_after_all_visual(self) -> None:
        """``launch_assembly`` happens once, after the batched visual launch."""
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=4
        )
        names = _names(script)
        last_visual = max(
            i for i, n in enumerate(names) if n == "launch_visual_production"
        )
        assembly = names.index("launch_assembly")
        assert assembly > last_visual

    def test_default_num_scenes_derives_from_duration(self) -> None:
        """When caller omits ``num_scenes``, N is derived from duration."""
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en"
        )
        # ceil(60 / 12) = 5.
        assert _names(script).count("launch_audio_render") == 5
        assert _names(script).count("launch_visual_production") == 5


# ---------------------------------------------------------------------------
# Orchestrator prompt anti-drift
# ---------------------------------------------------------------------------


class TestOrchestratorPromptMultiScene:
    """``ORCHESTRATOR_PROMPT`` must instruct the LLM to dispatch per-scene."""

    def test_prompt_mentions_multi_scene_iteration(self) -> None:
        prompt = pipeline_mod.ORCHESTRATOR_PROMPT
        assert "Multi-scene iteration discipline (slice 9f)" in prompt

    def test_prompt_mentions_per_scene_dispatch(self) -> None:
        prompt = pipeline_mod.ORCHESTRATOR_PROMPT
        assert "one tool call per" in prompt or "one call per scene" in prompt

    def test_prompt_mentions_parallel_audio_launch(self) -> None:
        prompt = pipeline_mod.ORCHESTRATOR_PROMPT
        assert "launch_audio_render" in prompt
        assert "parallel" in prompt.lower()

    def test_prompt_mentions_parallel_visual_launch(self) -> None:
        prompt = pipeline_mod.ORCHESTRATOR_PROMPT
        assert "launch_visual_production" in prompt
