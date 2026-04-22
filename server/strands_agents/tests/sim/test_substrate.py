"""Direct-proof tests for :class:`Substrate` — the composition layer.

The substrate's job is to flip every ``set_*_helpers`` entry point in
the pipeline so the fake for each channel is actually called. These
tests verify that contract by calling each helper after install and
observing the fake received the call (via the shared recorder).

The true orchestrator-trajectory tests — booting the real
:func:`create_deep_agent` with the fakes — land in PR-S3; these tests
stay at the thinnest possible layer above the ``set_*_helpers`` API so
they fail loudly when a new slot is added and the substrate forgets to
plumb it.
"""

from __future__ import annotations

import pytest

from strands_agents import (
    audio_tool as _audio_tool,
    coherence_evaluator as _coherence_evaluator,
    content_analyst as _content_analyst,
    scenario_agent as _scenario_agent,
    scenario_refiner as _scenario_refiner,
    task_tools as _task_tools,
    visual_concepter as _visual_concepter,
)
from strands_agents.sim.llm import LLMScript
from strands_agents.sim.substrate import Substrate
from strands_agents.tools import assembly_tool as _assembly_tool


@pytest.fixture
def substrate() -> Substrate:
    script = (
        LLMScript()
        .when_generate_scenario(
            response={"scenes": [{"id": "s1", "title": "t"}]}, reusable=True
        )
        .when_refine_scenario(response={"scenes": [{"id": "s1"}]}, reusable=True)
        .when_rewrite_voice_text(response="rewritten", reusable=True)
        .when_extract_phrases(response=[{"text": "p"}], reusable=True)
        .when_propose_concept(response={"concept_id": "c1"}, reusable=True)
        .when_score_coherence(response={"verdict": "GOOD"}, reusable=True)
    )
    s = Substrate(llm_script=script, workers_total=1)
    try:
        yield s
    finally:
        s.uninstall()
        s.shutdown()


class TestSubstrateInstallFlipsEveryHelper:
    """After :meth:`Substrate.install`, every module's private helper
    slot must point at a fake. We probe the module globals directly
    because each module stores its helper in a slightly different
    shape (``_GENERATOR``, ``_HELPERS`` dataclass, ``_helpers`` dict,
    etc.) — this is the most faithful way to catch a missed wiring.
    """

    @staticmethod
    def _bound_to(method: object, instance: object) -> bool:
        """Return True if ``method`` is a bound method of ``instance``.

        Bound-method identity (``is``) breaks because Python builds a
        fresh method wrapper on every attribute access. Compare the
        ``__self__`` instead.
        """
        return getattr(method, "__self__", None) is instance

    def test_scenario_agent_generator_and_refiner(
        self, substrate: Substrate
    ) -> None:
        assert _scenario_agent._GENERATOR is None
        assert _scenario_agent._REFINER is None
        with substrate.installed():
            assert self._bound_to(_scenario_agent._GENERATOR, substrate.llm)
            assert self._bound_to(_scenario_agent._REFINER, substrate.llm)
        assert _scenario_agent._GENERATOR is None
        assert _scenario_agent._REFINER is None

    def test_scenario_refiner_text_rewriter(self, substrate: Substrate) -> None:
        assert _scenario_refiner._TEXT_REWRITER is None
        with substrate.installed():
            assert self._bound_to(
                _scenario_refiner._TEXT_REWRITER, substrate.llm
            )
        assert _scenario_refiner._TEXT_REWRITER is None

    def test_audio_helpers_all_wired(self, substrate: Substrate) -> None:
        assert _audio_tool._HELPERS is None
        with substrate.installed():
            h = _audio_tool._HELPERS
            assert h is not None
            assert self._bound_to(h.tts_generate, substrate.tts)
            assert self._bound_to(h.whisperx_align, substrate.tts)
            assert self._bound_to(h.loudness_normalize, substrate.tts)
            assert self._bound_to(h.b2_upload, substrate.b2)
        assert _audio_tool._HELPERS is None

    def test_content_analyst_extractor(self, substrate: Substrate) -> None:
        assert _content_analyst._EXTRACTOR is None
        with substrate.installed():
            assert self._bound_to(_content_analyst._EXTRACTOR, substrate.llm)
        assert _content_analyst._EXTRACTOR is None

    def test_visual_concepter_proposer(self, substrate: Substrate) -> None:
        assert _visual_concepter._PROPOSER is None
        with substrate.installed():
            assert self._bound_to(_visual_concepter._PROPOSER, substrate.llm)
        assert _visual_concepter._PROPOSER is None

    def test_coherence_evaluator_scorer(self, substrate: Substrate) -> None:
        assert _coherence_evaluator._SCORER is None
        with substrate.installed():
            assert self._bound_to(_coherence_evaluator._SCORER, substrate.llm)
        assert _coherence_evaluator._SCORER is None

    def test_production_helpers_all_wired(self, substrate: Substrate) -> None:
        assert _task_tools._HELPERS is None
        with substrate.installed():
            h = _task_tools._HELPERS
            assert h is not None
            assert h.pool is substrate.pool
            assert self._bound_to(h.dispatch, substrate.renderer)
            assert self._bound_to(h.health_check, substrate.renderer)
        assert _task_tools._HELPERS is None

    def test_assembly_helpers_all_wired(self, substrate: Substrate) -> None:
        # Pre-install: every assembly slot is the not-wired raiser.
        snap_before = _assembly_tool._snapshot_helpers()
        with pytest.raises(RuntimeError, match="not wired"):
            snap_before["validate_timeline"]("irrelevant")

        with substrate.installed():
            snap = _assembly_tool._snapshot_helpers()
            # Validate the shape of each installed helper: compose
            # returns a path, validate returns a (passed, list) tuple,
            # upload returns a fake-b2 URL. render_final is tested via
            # its sentinel output in a separate case below.
            assert self._bound_to(snap["upload_to_b2"], substrate.b2)
            # validate + compose are private closures, so we verify
            # behaviourally rather than by identity.
            passed, violations = snap["validate_timeline"]("fake.otio")
            assert passed is True
            assert violations == []

        # Post-uninstall: back to not-wired raiser.
        snap_after = _assembly_tool._snapshot_helpers()
        with pytest.raises(RuntimeError, match="not wired"):
            snap_after["validate_timeline"]("irrelevant")


class TestSubstrateInstallIdempotent:
    def test_install_twice_is_noop(self, substrate: Substrate) -> None:
        substrate.install()
        substrate.install()  # must not raise

    def test_uninstall_twice_is_noop(self, substrate: Substrate) -> None:
        substrate.install()
        substrate.uninstall()
        substrate.uninstall()  # must not raise


class TestSubstrateFakesAreCalledThroughHelpers:
    """Drive a realistic call per module after install; verify the
    recorder captured the fake."""

    def test_scenario_helper_flow(self, substrate: Substrate) -> None:
        with substrate.installed():
            out = _scenario_agent._GENERATOR(
                "inflation", 3, "economic", "en"
            )
            assert out["scenes"][0]["id"] == "s1"
        assert substrate.recorder.count("llm", "generate_scenario") == 1

    def test_audio_helper_flow(self, substrate: Substrate) -> None:
        with substrate.installed():
            h = _audio_tool._HELPERS
            assert h is not None
            wav = h.tts_generate(1, "V1", "hello world", "en")
            seg = h.whisperx_align(wav, "hello world", "en")
            h.loudness_normalize(wav, -16.0)
            url = h.b2_upload(wav)
            assert seg["word_count"] == 2
            assert url.startswith("fake-b2://")
        assert substrate.recorder.count("tts", "tts_generate") == 1
        assert substrate.recorder.count("tts", "whisperx_align") == 1
        assert substrate.recorder.count("tts", "loudness_normalize") == 1
        assert substrate.recorder.count("b2", "upload") == 1

    def test_production_helper_flow(self, substrate: Substrate) -> None:
        with substrate.installed():
            h = _task_tools._HELPERS
            assert h is not None
            health = h.health_check()
            assert health["workers_total"] == 1
            payload = h.dispatch(
                scene_id="s1",
                concept_id="c1",
                prompt="a shot of coins",
                style_lock={"tokens": ["35mm"]},
                duration_sec=3.0,
                seed=1,
                audio_artifact_url="fake-b2://a/b.wav",
            )
            assert payload["frames"] == 3 * 24
        assert substrate.recorder.count("renderer", "health_check") == 1
        assert substrate.recorder.count("renderer", "dispatch") == 1

    def test_assembly_helper_flow(
        self, substrate: Substrate, tmp_path
    ) -> None:
        with substrate.installed():
            snap = _assembly_tool._snapshot_helpers()
            out_path = str(tmp_path / "out.otio")
            rv = snap["compose_timeline"](
                scenes=[{"id": "s1"}],
                clip_artifacts=[{"scene_id": "s1"}],
                whisperx_alignment={"word_count": 2},
                timeline_path="ignored",
                output_path=out_path,
            )
            assert rv == out_path
            passed, violations = snap["validate_timeline"](out_path)
            assert passed is True
            assert violations == []
            final = snap["render_final"](
                otio_path=out_path, output_dir=str(tmp_path / "renders")
            )
            assert final.endswith("final.mp4")
            final_url = snap["upload_to_b2"](final)
            assert final_url.startswith("fake-b2://")
        assert substrate.recorder.count("b2", "upload") >= 1


class TestSubstrateComposeTimelineGuard:
    def test_scene_clip_mismatch_fails_loudly(
        self, substrate: Substrate, tmp_path
    ) -> None:
        with substrate.installed():
            snap = _assembly_tool._snapshot_helpers()
            with pytest.raises(RuntimeError, match="does not match"):
                snap["compose_timeline"](
                    scenes=[{"id": "s1"}, {"id": "s2"}],
                    clip_artifacts=[{"scene_id": "s1"}],
                    whisperx_alignment={"word_count": 0},
                    timeline_path="ignored",
                    output_path=str(tmp_path / "out.otio"),
                )


class TestSubstrateRecorderGetsAllChannels:
    def test_one_recorder_captures_every_fake(
        self, substrate: Substrate, tmp_path
    ) -> None:
        with substrate.installed():
            _scenario_agent._GENERATOR("t", 1, "s", "en")
            _audio_tool._HELPERS.tts_generate(1, "V1", "hi", "en")
            _task_tools._HELPERS.health_check()
            substrate.b2.upload_bytes(b"x", basename="x.bin")
            substrate.clock.advance(1.0)
            substrate.interrupt.script(
                tool_name="launch_assembly", decision={"type": "accept"}
            )
            substrate.interrupt.next_decision("launch_assembly")

        channels = {r.channel for r in substrate.recorder.records}
        # Every channel must have at least one record — a missed
        # wiring would leave one of these empty.
        assert channels == {"llm", "tts", "renderer", "b2", "clock", "interrupt"}
