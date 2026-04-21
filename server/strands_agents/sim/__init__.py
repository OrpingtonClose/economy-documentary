"""Simulator substrate — the six fakes the orchestrator simulator plugs in.

This package exists so that :func:`strands_agents.pipeline.build_orchestrator`
can be booted in tests against fully fake IO without the orchestrator,
SubAgents, tool registry, memory files, or approval gates knowing
anything about the swap. The substrate stands on two ideas:

1. **Single IO boundary.** Every external call the pipeline makes during a
   run reaches the outside world through exactly one of six channels —
   LLM, TTS, GPU renderer, B2 object storage, wall-clock time, or the
   human operator console. One fake per channel is enough.

2. **Scripting, not mocking.** Each fake is driven by a declarative
   *script* that describes the scenario under test (e.g. "the scenario
   generator produces POOR on call 1 and GOOD on call 2", "clip 3
   comes back with a frozen frame, clip 4 clean"). Tests build a
   script, call :meth:`Substrate.install`, run the real orchestrator,
   then read :class:`Recorder` output for trajectory assertions.

Public API:

* :class:`FakeClock` — monotonic deterministic time.
* :class:`FakeB2` — in-memory object store behind an upload/get interface.
* :class:`FakeLLM` — scripted responses for every LLM-backed helper
  (scenario generator, refiner, text rewriter, phrase extractor,
  concept proposer, coherence scorer).
* :class:`FakeTTS` — WAV synthesis + WhisperX alignment + loudness
  normalisation. Audio is silent but structurally valid; alignment
  timings are scriptable to force timing-loop failures.
* :class:`FakeRenderer` — GPU worker dispatch + health check. Per-scene
  outcomes (clean / frozen-frame / black-frames / wrong-duration) are
  scriptable.
* :class:`FakeInterrupt` — scripted operator decisions for approval
  gates.
* :class:`Recorder` — captures every fake call in order. Trajectory
  tests read this.
* :class:`Substrate` — owns all six fakes + the recorder, and knows how
  to plumb them into every ``set_*_helpers`` entry point in one call.
"""

from __future__ import annotations

from strands_agents.sim.b2 import FakeB2
from strands_agents.sim.clock import FakeClock
from strands_agents.sim.interrupt import FakeInterrupt, NoScriptedDecision
from strands_agents.sim.llm import FakeLLM, NoScriptedResponse
from strands_agents.sim.recorder import CallRecord, Recorder
from strands_agents.sim.renderer import FakeRenderer, RenderOutcome
from strands_agents.sim.substrate import Substrate
from strands_agents.sim.tts import FakeTTS

__all__ = [
    "CallRecord",
    "FakeB2",
    "FakeClock",
    "FakeInterrupt",
    "FakeLLM",
    "FakeRenderer",
    "FakeTTS",
    "NoScriptedDecision",
    "NoScriptedResponse",
    "Recorder",
    "RenderOutcome",
    "Substrate",
]
