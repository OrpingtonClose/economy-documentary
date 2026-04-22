"""The composition layer that wires all six fakes into the real pipeline.

A :class:`Substrate` owns one of each fake plus a shared :class:`Recorder`,
and knows the full set of ``set_*_helpers`` entry points the pipeline
exposes. :meth:`install` flips every injection slot to the fake; the
context manager form (``with substrate.installed(): ...``) also
guarantees :meth:`uninstall` runs on exit so test isolation holds even
if the body raises.

The assembly tool and the production SubAgent both need more than one
fake (assembly needs B2 + a scripted OTIO composer + validator;
production needs a real :class:`AsyncTaskPool` + the renderer's
dispatch). The substrate constructs those glue pieces up-front so a
test author passes nothing except scripts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from strands_agents import (
    audio_tool as _audio_tool,
    coherence_evaluator as _coherence_evaluator,
    content_analyst as _content_analyst,
    scenario_agent as _scenario_agent,
    scenario_refiner as _scenario_refiner,
    task_tools as _task_tools,
    visual_concepter as _visual_concepter,
)
from strands_agents.sim.b2 import FakeB2
from strands_agents.sim.clock import FakeClock
from strands_agents.sim.interrupt import FakeInterrupt
from strands_agents.sim.llm import FakeLLM, LLMScript
from strands_agents.sim.recorder import Recorder
from strands_agents.sim.renderer import FakeRenderer
from strands_agents.sim.tts import FakeTTS
from strands_agents.tools import assembly_tool as _assembly_tool
from strands_agents.tools.task_pool import AsyncTaskPool


class Substrate:
    """All six fakes + a recorder, ready to plug into the real pipeline."""

    def __init__(
        self,
        *,
        llm_script: LLMScript | None = None,
        workers_total: int = 2,
    ) -> None:
        """Build a substrate with fresh fakes.

        Args:
            llm_script: Optional :class:`LLMScript` for the LLM fake.
                Rules can still be appended after construction via
                ``substrate.llm._script.when_*``.
            workers_total: Initial GPU worker count reported by the
                renderer's ``health_check``. Set this matching the
                test scenario you want to simulate (1 for a
                single-worker pool, higher for stress tests).
        """
        self.recorder = Recorder()
        self.clock = FakeClock(recorder=self.recorder)
        self.b2 = FakeB2(recorder=self.recorder)
        self.llm = FakeLLM(llm_script, recorder=self.recorder)
        self.tts = FakeTTS(recorder=self.recorder)
        self.renderer = FakeRenderer(
            recorder=self.recorder, workers_total=workers_total
        )
        self.interrupt = FakeInterrupt(recorder=self.recorder)

        # A real AsyncTaskPool — the production SubAgent expects one
        # and it's deterministic at small worker counts.
        self.pool = AsyncTaskPool(max_workers=workers_total)

        self._installed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def install(self) -> None:
        """Plumb every fake into every ``set_*_helpers`` entry point.

        Safe to call twice; the second call is a no-op. Callers
        should always pair this with :meth:`uninstall` so module-level
        helper state doesn't leak between tests.
        """
        if self._installed:
            return
        # Component 01 — scenario_agent
        _scenario_agent.set_scenario_helpers(
            generator=self.llm.generate_scenario,
            refiner=self.llm.refine_scenario,
        )
        # Component 03 — scenario_refiner
        _scenario_refiner.set_refiner_helpers(
            text_rewriter=self.llm.rewrite_voice_text,
        )
        # Component 04 — audio_tool
        _audio_tool.set_audio_helpers(
            tts_generate=self.tts.tts_generate,
            whisperx_align=self.tts.whisperx_align,
            loudness_normalize=self.tts.loudness_normalize,
            b2_upload=self.b2.upload,
        )
        # Component 06 — content_analyst
        _content_analyst.set_content_analyst_helpers(
            phrase_extractor=self.llm.extract_phrases,
        )
        # Component 07 — visual_concepter
        _visual_concepter.set_visual_concepter_helpers(
            concept_proposer=self.llm.propose_concept,
        )
        # Component 08 — coherence_evaluator
        _coherence_evaluator.set_coherence_evaluator_helpers(
            soft_scorer=self.llm.score_coherence,
        )
        # Component 10 — task_tools / production SubAgent
        _task_tools.set_production_helpers(
            pool=self.pool,
            dispatch=self.renderer.dispatch,
            health_check=self.renderer.health_check,
        )
        # Component 11 — assembly_tool
        _assembly_tool.set_assembly_helpers(
            compose_timeline=_compose_timeline_fake,
            validate_timeline=_validate_timeline_fake,
            render_final=_render_final_fake(self.renderer),
            upload_to_b2=self.b2.upload,
        )
        self._installed = True

    def uninstall(self) -> None:
        """Restore every injection slot to its not-wired default."""
        if not self._installed:
            return
        _scenario_agent.clear_scenario_helpers()
        _scenario_refiner.clear_refiner_helpers()
        _audio_tool.clear_audio_helpers()
        _content_analyst.clear_content_analyst_helpers()
        _visual_concepter.clear_visual_concepter_helpers()
        _coherence_evaluator.clear_coherence_evaluator_helpers()
        _task_tools.clear_production_helpers()
        _assembly_tool.reset_assembly_helpers()
        self._installed = False

    @contextmanager
    def installed(self) -> Iterator[Substrate]:
        """Install on entry, uninstall on exit, even on exception."""
        self.install()
        try:
            yield self
        finally:
            self.uninstall()

    def shutdown(self) -> None:
        """Release the real :class:`AsyncTaskPool`. Call at the end of a
        test run."""
        self.pool.shutdown(wait_for_completion=False)


# ---------------------------------------------------------------------------
# Assembly-tool glue — the assembly atoms (OTIO compose, validate, render)
# are filesystem-level helpers rather than LLM calls, so they live here
# instead of in FakeLLM or FakeRenderer. Kept module-private; tests that
# want to exercise OTIO failure paths can wrap these or swap them in via
# set_assembly_helpers directly after install().
# ---------------------------------------------------------------------------


def _compose_timeline_fake(
    *,
    scenes: list[dict[str, Any]],
    clip_artifacts: list[dict[str, Any]],
    whisperx_alignment: dict[str, Any],
    timeline_path: str,  # noqa: ARG001 — matches protocol, not used by fake
    output_path: str,
) -> str:
    """Write a tiny sentinel OTIO-shaped JSON and return ``output_path``.

    Real composition is tested elsewhere; for trajectory tests we just
    need the call to succeed with a stable output path and to fail
    loudly when scene/clip counts disagree (which is the only invariant
    the orchestrator itself cares about when deciding whether to
    proceed to final render).
    """
    import json
    import os

    if len(scenes) != len(clip_artifacts):
        msg = (
            f"fake compose_timeline: scene count {len(scenes)} does not match "
            f"clip count {len(clip_artifacts)}"
        )
        raise RuntimeError(msg)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "fake_otio": True,
                "scene_count": len(scenes),
                "clip_count": len(clip_artifacts),
                "word_count": whisperx_alignment.get("word_count", 0),
            },
            fh,
        )
    return output_path


def _validate_timeline_fake(
    otio_path: str,  # noqa: ARG001 — fake never fails validation by default
) -> tuple[bool, list[dict[str, Any]]]:
    """Assume the fake OTIO is always valid. Tests that want to
    exercise validation-failure paths install their own validator on
    top via :func:`set_assembly_helpers`."""
    return True, []


def _render_final_fake(
    renderer: FakeRenderer,
) -> Callable[..., str]:
    """Return a ``render_final`` helper that writes a small mp4-shaped
    sentinel under the renderer's tmpdir and returns the path."""
    import os

    def _render(*, otio_path: str, output_dir: str) -> str:
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, "final.mp4")
        with open(out_path, "wb") as fh:
            fh.write(f"fake-final-mp4|from={otio_path}".encode())
        # Access the renderer to make the dependency explicit — also
        # lets a future version log through the same recorder if we
        # decide final renders should show up in the trajectory.
        _ = renderer
        return out_path

    return _render
