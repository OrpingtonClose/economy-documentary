"""Trajectory tests for the audio-worker hard invariant.

AGENTS.md, invariant #1 (audio-worker scheduling):

    One TTS voice per VM. The TTS worker is stateful. A single VM
    generates audio for exactly one character voice. Launching two
    ``launch_audio_render`` tasks against the same worker pool with
    different character voices is a race. If two voices are required,
    they run on distinct worker pools.

These tests drive the real DeepAgent orchestrator through scripted
scenarios via :class:`OrchestratorSimulator`, extract the tool-call
trajectory with :func:`tool_call_trajectory`, and grade the trajectory
with :class:`AudioWorkerInvariantEvaluator`. Each scenario pins one
branch of the invariant:

* **Happy path (single voice)** — many scenes in one parallel batch,
  all voice ``V1``. Gates pass.
* **Serialised multi-voice** — voice ``V1`` in one batch, voice ``V2``
  in a later batch. Gates pass (no pool rebind, no mixed-voice batch).
* **Cross-voice race in one batch** — voices ``V1`` and ``V2`` in the
  same parallel batch. Batch gate fails (the orchestrator cannot
  prove distinct VMs handled each call).
* **Distinct worker_pool per voice in one batch** — voice ``V1`` on
  ``pool-a``, voice ``V2`` on ``pool-b``, both emitted in the same
  batch. Batch gate still fails (see the evaluator docstring) but
  the pool-rebind gate passes.
* **Pool rebind across turns** — voice ``V1`` on ``pool-a`` in turn
  1, voice ``V2`` on ``pool-a`` in turn 3. Batch gate passes (each
  batch is single-voice), pool-rebind gate fails.
* **Expected cross-voice race** — same scripted violation as the
  cross-voice-race case, but with ``expect_cross_voice_race=True``
  in the evaluator metadata. Gate passes because the violation was
  expected.

The stub tools used here are deliberately minimal: they accept the
fields the evaluator reads and return a short string so the scripted
LLM turn can advance to the next message. No TTS, no GPU, no real
alignment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool

from strands_agents.evals.evaluators.audio_worker_invariant import (
    AudioWorkerInvariantEvaluator,
)
from strands_agents.sim import (
    OrchestratorSimulator,
    SimulationResult,
    scripted_final,
    scripted_parallel_tool_calls,
    scripted_tool_call,
    tool_call_trajectory,
)
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput


# ---------------------------------------------------------------------------
# Stub tools. Signatures cover every argument the evaluator inspects
# (``scene_id``, ``voice_id``, ``worker_pool``) plus the awaiter.
# ---------------------------------------------------------------------------


@tool
def launch_audio_render(
    scene_id: str,
    voice_id: str,
    worker_pool: str = "",
) -> str:
    """Stub for the real ``launch_audio_render`` tool."""
    return f"launched:{scene_id}:{voice_id}:{worker_pool}"


@tool
def await_tasks(task_ids: list[str] | None = None) -> str:
    """Stub for the real ``await_tasks`` tool."""
    return "ok"


_ALL_STUB_TOOLS = [launch_audio_render, await_tasks]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def simulator() -> Iterator[OrchestratorSimulator]:
    sim = OrchestratorSimulator()
    sim.with_tools(_ALL_STUB_TOOLS)
    try:
        yield sim
    finally:
        sim.shutdown()


def _run(sim: OrchestratorSimulator, brief: str, run_dir: Path) -> SimulationResult:
    return asyncio.run(sim.run(brief, run_dir))


def _gate(
    outputs: list[EvaluationOutput],
    label: str,
) -> EvaluationOutput:
    """Find an evaluator output by label. Fails the test on missing labels."""
    for output in outputs:
        if output.label == label:
            return output
    labels = [o.label for o in outputs]
    pytest.fail(f"missing evaluator gate {label!r}; got {labels!r}")


def _launch(scene_id: str, voice_id: str, worker_pool: str | None = None) -> tuple[str, dict[str, Any]]:
    args: dict[str, Any] = {"scene_id": scene_id, "voice_id": voice_id}
    if worker_pool is not None:
        args["worker_pool"] = worker_pool
    return "launch_audio_render", args


def _await() -> Any:
    return scripted_tool_call("await_tasks", {"task_ids": []})


def _grade(result: SimulationResult, **metadata: Any) -> list[EvaluationOutput]:
    trajectory = tool_call_trajectory(result)
    return AudioWorkerInvariantEvaluator().evaluate(
        EvaluationData(
            input="brief",
            actual_trajectory=trajectory,
            metadata=metadata or None,
        )
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSingleVoiceHappyPath:
    """One voice across several scenes in a single parallel batch."""

    def test_three_scenes_single_voice_passes(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                scripted_parallel_tool_calls(
                    _launch("s1", "V1"),
                    _launch("s2", "V1"),
                    _launch("s3", "V1"),
                ),
                _await(),
                scripted_final("audio batch complete"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        assert _gate(outputs, "audio_worker.voice_id_present").test_pass
        assert _gate(outputs, "audio_worker.no_cross_voice_in_batch").test_pass


class TestSerialisedMultiVoice:
    """Two voices, each in its own batch. The clean multi-voice pattern."""

    def test_two_voices_across_turns_passes(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                scripted_parallel_tool_calls(
                    _launch("s1", "V1"),
                    _launch("s2", "V1"),
                ),
                _await(),
                scripted_parallel_tool_calls(
                    _launch("s3", "V2"),
                    _launch("s4", "V2"),
                ),
                _await(),
                scripted_final("two voices rendered"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        assert _gate(outputs, "audio_worker.voice_id_present").test_pass
        assert _gate(outputs, "audio_worker.no_cross_voice_in_batch").test_pass


class TestCrossVoiceRace:
    """Two voices in the same parallel batch. The forbidden pattern."""

    def test_mixed_voice_batch_fails_without_expectation(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                scripted_parallel_tool_calls(
                    _launch("s1", "V1"),
                    _launch("s2", "V2"),
                ),
                _await(),
                scripted_final("race attempted"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        # Voice ids are still present — every call had one.
        assert _gate(outputs, "audio_worker.voice_id_present").test_pass
        # But mixing them in one parallel batch is the race.
        gate = _gate(outputs, "audio_worker.no_cross_voice_in_batch")
        assert gate.test_pass is False
        assert "V1" in gate.reason and "V2" in gate.reason

    def test_mixed_voice_batch_passes_when_expected(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                scripted_parallel_tool_calls(
                    _launch("s1", "V1"),
                    _launch("s2", "V2"),
                ),
                _await(),
                scripted_final("race attempted"),
            ]
        )
        outputs = _grade(
            _run(simulator, "brief", run_dir),
            expect_cross_voice_race=True,
        )

        gate = _gate(outputs, "audio_worker.no_cross_voice_in_batch")
        assert gate.test_pass is True


class TestPoolRebind:
    """Same worker_pool asked to render two voices at different turns."""

    def test_pool_rebind_across_turns_fails(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                scripted_parallel_tool_calls(
                    _launch("s1", "V1", worker_pool="pool-a"),
                    _launch("s2", "V1", worker_pool="pool-a"),
                ),
                _await(),
                scripted_parallel_tool_calls(
                    _launch("s3", "V2", worker_pool="pool-a"),
                ),
                _await(),
                scripted_final("pool rebound"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        # Each batch is single-voice so the batch gate passes.
        assert _gate(outputs, "audio_worker.no_cross_voice_in_batch").test_pass
        # But pool-a was rebound from V1 to V2.
        gate = _gate(outputs, "audio_worker.no_pool_rebind")
        assert gate.test_pass is False
        assert "pool-a" in gate.reason

    def test_distinct_pool_per_voice_across_turns_passes(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                scripted_parallel_tool_calls(
                    _launch("s1", "V1", worker_pool="pool-a"),
                    _launch("s2", "V1", worker_pool="pool-a"),
                ),
                _await(),
                scripted_parallel_tool_calls(
                    _launch("s3", "V2", worker_pool="pool-b"),
                ),
                _await(),
                scripted_final("two-pool render"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        assert _gate(outputs, "audio_worker.no_cross_voice_in_batch").test_pass
        assert _gate(outputs, "audio_worker.no_pool_rebind").test_pass


class TestDistinctPoolsInOneBatch:
    """Two pools in one parallel batch with different voices.

    Even with disjoint ``worker_pool`` arguments, emitting both calls
    in one tool-call turn still trips the batch gate — the evaluator
    cannot prove the tool runner actually respects pool routing when
    calls race inside a single turn. The pool-rebind gate, however,
    still passes because each pool stays bound to exactly one voice.
    """

    def test_distinct_pools_same_turn_fails_batch_passes_rebind(
        self,
        simulator: OrchestratorSimulator,
        run_dir: Path,
    ) -> None:
        simulator.with_chat_responses(
            [
                scripted_parallel_tool_calls(
                    _launch("s1", "V1", worker_pool="pool-a"),
                    _launch("s2", "V2", worker_pool="pool-b"),
                ),
                _await(),
                scripted_final("cross-pool batch"),
            ]
        )
        outputs = _grade(_run(simulator, "brief", run_dir))

        assert _gate(outputs, "audio_worker.no_cross_voice_in_batch").test_pass is False
        assert _gate(outputs, "audio_worker.no_pool_rebind").test_pass is True
