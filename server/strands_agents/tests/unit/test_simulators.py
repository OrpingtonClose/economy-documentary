"""Unit tests for the :mod:`strands_agents.evals.simulators` configs.

These tests exercise the simulator *wiring* (tool registration,
pydantic schemas, share-state ids) without calling an LLM — that would
require model credentials and belongs in integration tests. We verify
that each simulator registers the expected tool names against the
shared :class:`StateRegistry`, matching the ``SIMULATION.md`` spec.
"""

from __future__ import annotations

from strands_evals.simulation.tool_simulator import StateRegistry

from strands_agents.evals.simulators.escalation_actor import (
    ESCALATION_CASES,
    MAX_TURNS,
)
from strands_agents.evals.simulators.gpu_worker import (
    DispatchResponse,
    JobStatus,
    WorkerHealth,
    build_gpu_worker_simulator,
)
from strands_agents.evals.simulators.tts_worker import (
    TtsHealth,
    TtsResponse,
    WhisperXResponse,
    build_tts_worker_simulator,
)


def test_gpu_worker_registers_expected_tools() -> None:
    sim = build_gpu_worker_simulator()
    names = set(sim.list_tools())
    assert names == {"dispatch_video_job", "check_job_status", "check_worker_health"}


def test_gpu_worker_pydantic_schemas_are_consistent() -> None:
    # Regression guard: these contracts are referenced by evaluator
    # metadata in downstream experiments. A field rename here is a
    # breaking change.
    assert set(DispatchResponse.model_fields) == {"job_id", "worker_url", "queued_at"}
    assert set(JobStatus.model_fields) >= {"job_id", "state", "progress"}
    assert set(WorkerHealth.model_fields) == {
        "status",
        "capabilities",
        "gpu_mem_free_mb",
        "queue_depth",
    }


def test_tts_worker_registers_expected_tools() -> None:
    sim = build_tts_worker_simulator()
    names = set(sim.list_tools())
    assert names == {"generate_tts", "align_whisperx", "check_tts_health"}


def test_tts_worker_pydantic_schemas_are_consistent() -> None:
    assert set(TtsResponse.model_fields) == {
        "wav_path",
        "duration_sec",
        "voice_id",
        "sample_rate",
    }
    assert set(WhisperXResponse.model_fields) == {
        "word_timestamps",
        "total_duration_sec",
        "language",
    }
    assert set(TtsHealth.model_fields) == {
        "status",
        "loaded_model",
        "voice_ids_available",
    }


def test_gpu_and_tts_simulators_can_share_state_registry() -> None:
    # Crucial invariant: one registry per experiment, shared across
    # audio + video pipelines so cross-contamination between the two
    # would fail eagerly instead of propagating bad state.
    registry = StateRegistry()
    gpu = build_gpu_worker_simulator(state_registry=registry)
    tts = build_tts_worker_simulator(state_registry=registry)
    all_names = set(gpu.list_tools()) | set(tts.list_tools())
    assert len(all_names) == 6


def test_escalation_catalogue_matches_simulation_md_spec() -> None:
    expected_names = {
        "transient_error_retry",
        "persistent_error_escalate",
        "fixable_error_with_hint",
        "catastrophic_error_abort",
        "confusing_mixed_signal",
        "user_overrides_suggestion",
        "user_requests_diagnostic",
        "unresponsive_user",
    }
    assert {c.name for c in ESCALATION_CASES} == expected_names
    assert MAX_TURNS == 8


def test_every_escalation_case_has_expected_outcome_metadata() -> None:
    for case in ESCALATION_CASES:
        assert case.metadata is not None
        assert "expected_outcome" in case.metadata
        assert case.expected_output
