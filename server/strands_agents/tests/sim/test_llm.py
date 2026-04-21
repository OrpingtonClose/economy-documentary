"""Direct-proof tests for :class:`FakeLLM`."""

from __future__ import annotations

import pytest

from strands_agents.sim.llm import FakeLLM, LLMScript, NoScriptedResponse
from strands_agents.sim.recorder import Recorder


class TestFakeLLMDispatch:
    def test_generate_scenario_returns_scripted_response(self) -> None:
        canned = {"scenes": [{"id": "s1", "title": "intro"}], "revision": "abc"}
        script = LLMScript().when_generate_scenario(response=canned)
        llm = FakeLLM(script)
        out = llm.generate_scenario(
            topic="inflation", num_scenes=1, style="economic", language="en"
        )
        # Value equality — the fake deep-copies scripted responses so
        # caller-side mutations never poison future calls.
        assert out == canned
        assert out is not canned

    def test_no_script_raises(self) -> None:
        llm = FakeLLM()
        with pytest.raises(NoScriptedResponse, match="generate_scenario"):
            llm.generate_scenario(
                topic="x", num_scenes=1, style="y", language="en"
            )

    def test_rule_fires_once_by_default(self) -> None:
        script = LLMScript().when_refine_scenario(response={"scenes": []})
        llm = FakeLLM(script)
        llm.refine_scenario(scenes=[], feedback={})
        with pytest.raises(NoScriptedResponse):
            llm.refine_scenario(scenes=[], feedback={})

    def test_reusable_rule_fires_many_times(self) -> None:
        script = LLMScript().when_refine_scenario(
            response={"scenes": [{"id": "s1"}]}, reusable=True
        )
        llm = FakeLLM(script)
        for _ in range(5):
            out = llm.refine_scenario(scenes=[], feedback={})
            assert out == {"scenes": [{"id": "s1"}]}

    def test_reusable_rule_returns_isolated_copies(self) -> None:
        # Mutating one response must not poison subsequent calls — the
        # pipeline routinely annotates LLM outputs with revision tags,
        # and we must not let those annotations leak across calls.
        script = LLMScript().when_refine_scenario(
            response={"scenes": [{"id": "s1"}]}, reusable=True
        )
        llm = FakeLLM(script)
        first = llm.refine_scenario(scenes=[], feedback={})
        first["metadata"] = {"mutated": True}
        first["scenes"].append({"id": "extra"})
        second = llm.refine_scenario(scenes=[], feedback={})
        assert second == {"scenes": [{"id": "s1"}]}
        assert "metadata" not in second

    def test_rules_picked_in_declaration_order(self) -> None:
        script = (
            LLMScript()
            .when_generate_scenario(response={"scenes": [{"id": "first"}]})
            .when_generate_scenario(response={"scenes": [{"id": "second"}]})
        )
        llm = FakeLLM(script)
        first = llm.generate_scenario(
            topic="x", num_scenes=1, style="y", language="en"
        )
        second = llm.generate_scenario(
            topic="x", num_scenes=1, style="y", language="en"
        )
        assert first["scenes"][0]["id"] == "first"
        assert second["scenes"][0]["id"] == "second"

    def test_match_predicate_filters(self) -> None:
        script = (
            LLMScript()
            .when_generate_scenario(
                response={"scenes": [{"id": "en"}]},
                match=lambda p: p["language"] == "en",
            )
            .when_generate_scenario(
                response={"scenes": [{"id": "es"}]},
                match=lambda p: p["language"] == "es",
            )
        )
        llm = FakeLLM(script)
        assert llm.generate_scenario("x", 1, "y", "es")["scenes"][0]["id"] == "es"
        assert llm.generate_scenario("x", 1, "y", "en")["scenes"][0]["id"] == "en"

    def test_all_helpers_dispatch(self) -> None:
        # One rule per helper, prove every entry point plumbs through.
        script = (
            LLMScript()
            .when_generate_scenario(response={"scenes": []})
            .when_refine_scenario(response={"scenes": []})
            .when_rewrite_voice_text(response="rewritten")
            .when_extract_phrases(response=[{"text": "p"}])
            .when_propose_concept(response={"concept_id": "c1"})
            .when_score_coherence(response={"verdict": "GOOD"})
        )
        llm = FakeLLM(script)
        assert llm.generate_scenario("t", 1, "s", "en") == {"scenes": []}
        assert llm.refine_scenario(scenes=[], feedback={}) == {"scenes": []}
        assert llm.rewrite_voice_text("hi", "shorter", -1.0) == "rewritten"
        assert llm.extract_phrases(scene={}, whisperx_segment={}, max_phrases=3) == [
            {"text": "p"}
        ]
        assert llm.propose_concept(phrase={}, style_lock={}, visual_style={}) == {
            "concept_id": "c1"
        }
        assert llm.score_coherence(
            visual_concepts=[], style_lock={}, content_analysis={}
        ) == {"verdict": "GOOD"}


class TestFakeLLMRecording:
    def test_every_call_recorded(self) -> None:
        r = Recorder()
        script = (
            LLMScript()
            .when_generate_scenario(response={"scenes": []})
            .when_refine_scenario(response={"scenes": []})
        )
        llm = FakeLLM(script, recorder=r)
        llm.generate_scenario("t", 1, "s", "en")
        llm.refine_scenario(scenes=[], feedback={})
        ops = r.ops(channel="llm")
        assert ops == ["generate_scenario", "refine_scenario"]
