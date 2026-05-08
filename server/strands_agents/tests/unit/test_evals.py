"""Smoke tests for the eval experiment factories.

Every experiment factory must:
1. Be importable without LLM credentials.
2. Return a non-empty Experiment with at least one case.
3. Return an Experiment with at least one evaluator.

These tests catch wiring regressions (broken imports, missing
evaluators, empty case lists) before a component PR lands.
They do NOT run the experiments — that requires LLM credentials
and happens in the playground or CI with model keys.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any, Callable

import pytest

from strands_evals.experiment import Experiment


# ---------------------------------------------------------------------------
# Experiment registry: (name, module_path, function_name, min_python)
# ---------------------------------------------------------------------------

# Each entry is (human_name, dotted_module, builder_function_name, min_python).
# We import directly from submodules to avoid coupling the test to
# the __init__.py re-export list.

_EXPERIMENT_REGISTRY: list[tuple[str, str, str, tuple[int, ...]]] = [
    # Core components
    ("smoke", "strands_agents.evals.experiments.smoke", "build_smoke_experiment", (3, 11)),
    ("scenario", "strands_agents.evals.experiments.scenario", "build_scenario_experiment", (3, 11)),
    ("audio", "strands_agents.evals.experiments.audio", "build_audio_experiment", (3, 11)),
    ("assembly", "strands_agents.evals.experiments.assembly", "build_assembly_experiment", (3, 11)),
    ("approval", "strands_agents.evals.experiments.approval", "build_approval_experiment", (3, 11)),
    ("escalation", "strands_agents.evals.experiments.escalation", "build_escalation_experiment", (3, 11)),
    ("escalation_contract", "strands_agents.evals.experiments.escalation", "build_escalation_contract_experiment", (3, 11)),
    ("recovery", "strands_agents.evals.experiments.recovery", "build_recovery_experiment", (3, 11)),
    ("recovery_classifier_contract", "strands_agents.evals.experiments.recovery", "build_recovery_classifier_contract_experiment", (3, 11)),
    ("recovery_remanifester_contract", "strands_agents.evals.experiments.recovery", "build_recovery_remanifester_contract_experiment", (3, 11)),
    ("production", "strands_agents.evals.experiments.production", "build_production_experiment", (3, 11)),
    ("timing", "strands_agents.evals.experiments.timing", "build_experiment", (3, 11)),
    ("timing_loop", "strands_agents.evals.experiments.timing_loop", "build_timing_loop_experiment", (3, 11)),
    ("visual_concepter", "strands_agents.evals.experiments.visual_concepter", "build_visual_concepter_experiment", (3, 11)),
    ("visual_loop", "strands_agents.evals.experiments.visual_loop", "build_visual_loop_experiment", (3, 11)),
    ("coherence_evaluator", "strands_agents.evals.experiments.coherence_evaluator", "build_coherence_evaluator_experiment", (3, 11)),
    ("content_analyst", "strands_agents.evals.experiments.content_analyst", "build_content_analyst_experiment", (3, 11)),
    ("scenario_refiner", "strands_agents.evals.experiments.scenario_refiner", "build_refiner_experiment", (3, 11)),
    ("pipeline", "strands_agents.evals.experiments.pipeline", "build_pipeline_experiment", (3, 11)),
    # Infra
    ("infra_agent", "strands_agents.evals.experiments.infra_agent", "build_infra_agent_experiment", (3, 11)),
    ("infra_b2_checkpoint", "strands_agents.evals.experiments.infra_b2_checkpoint", "build_infra_b2_checkpoint_experiment", (3, 11)),
    ("infra_guardian", "strands_agents.evals.experiments.infra_guardian", "build_infra_guardian_experiment", (3, 11)),
    ("infra_ltx_video_worker", "strands_agents.evals.experiments.infra_ltx_video_worker", "build_infra_ltx_video_worker_experiment", (3, 11)),
    ("infra_qwen3_tts_worker", "strands_agents.evals.experiments.infra_qwen3_tts_worker", "build_infra_qwen3_tts_worker_experiment", (3, 11)),
    ("infra_worker_registry", "strands_agents.evals.experiments.infra_worker_registry", "build_infra_worker_registry_experiment", (3, 11)),
    ("infra_pipeline_adapter", "strands_agents.evals.experiments.infra_pipeline_adapter", "build_infra_pipeline_adapter_experiment", (3, 11)),
    # Requires 3.12+ (pydantic TypedDict in deepagents)
    ("infra_pipeline_live_orchestrator", "strands_agents.evals.experiments.infra_pipeline_live_orchestrator", "build_infra_pipeline_live_orchestrator_experiment", (3, 12)),
    ("infra_ltx_video_worker_live", "strands_agents.evals.experiments.infra_ltx_video_worker_live", "build_infra_ltx_video_worker_live_experiment", (3, 12)),
    # Playground experiments (require 3.12+ due to deepagents pydantic chain)
    ("playground_catalog", "strands_agents.evals.experiments.playground_catalog", "build_playground_catalog_experiment", (3, 12)),
    ("playground_run", "strands_agents.evals.experiments.playground_run", "build_playground_run_experiment", (3, 12)),
    ("playground_user_cases", "strands_agents.evals.experiments.playground_user_cases", "build_playground_user_cases_experiment", (3, 12)),
    ("playground_evaluate", "strands_agents.evals.experiments.playground_evaluate", "build_playground_evaluate_experiment", (3, 12)),
    ("playground_reachability", "strands_agents.evals.experiments.playground_reachability", "build_playground_reachability_experiment", (3, 12)),
    ("playground_tasks", "strands_agents.evals.experiments.playground_tasks", "build_playground_tasks_experiment", (3, 12)),
    # Requires judge_model kwarg — tested separately
    ("escalation_judge", "strands_agents.evals.experiments.escalation", "build_escalation_judge_experiment", (3, 12)),
]


def _build_experiment(name: str) -> Experiment:
    """Import and call the build function for the named experiment."""
    for human_name, module_path, func_name, min_py in _EXPERIMENT_REGISTRY:
        if human_name == name:
            if sys.version_info < min_py:
                pytest.skip(f"requires Python >={min_py[0]}.{min_py[1]}")
            mod = importlib.import_module(module_path)
            builder: Callable[[], Experiment] = getattr(mod, func_name)
            # Some builders require kwargs (e.g. escalation_judge needs judge_model)
            import inspect
            sig = inspect.signature(builder)
            if any(
                p.name == "judge_model"
                for p in sig.parameters.values()
                if p.default is inspect.Parameter.empty
            ):
                pytest.skip("requires judge_model kwarg (LLM-backed)")
            return builder()
    raise ValueError(f"unknown experiment: {name!r}")


@pytest.fixture(
    params=[name for name, _, _, min_py in _EXPERIMENT_REGISTRY if sys.version_info >= min_py],
    ids=[name for name, _, _, min_py in _EXPERIMENT_REGISTRY if sys.version_info >= min_py],
)
def experiment_name(request: pytest.FixtureRequest) -> str:
    return request.param


def test_experiment_imports(experiment_name: str) -> None:
    """Every experiment factory must be importable without errors."""
    experiment = _build_experiment(experiment_name)
    assert isinstance(experiment, Experiment)


def test_experiment_has_cases(experiment_name: str) -> None:
    """Every experiment must have at least one case."""
    experiment = _build_experiment(experiment_name)
    assert len(experiment.cases) >= 1, (
        f"experiment {experiment_name!r} has no cases"
    )


def test_experiment_has_evaluators(experiment_name: str) -> None:
    """Every experiment must have at least one evaluator."""
    experiment = _build_experiment(experiment_name)
    assert len(experiment.evaluators) >= 1, (
        f"experiment {experiment_name!r} has no evaluators"
    )


# ---------------------------------------------------------------------------
# AG-UI mapping coverage
# ---------------------------------------------------------------------------

def test_agui_mapping_covers_known_kinds() -> None:
    """Every playground event kind must have an AG-UI mapping."""
    from strands_agents.playground.agui import KNOWN_KINDS, _KIND_TO_AGUI

    for kind in KNOWN_KINDS:
        assert kind in _KIND_TO_AGUI, f"kind {kind!r} missing from _KIND_TO_AGUI"


def test_agui_envelope_returns_type() -> None:
    """agui_envelope must always return a dict with a type key."""
    from strands_agents.playground.agui import agui_envelope

    for kind in ("run.dispatched", "tool.called", "narrate", "unknown.kind"):
        result = agui_envelope(kind)
        assert isinstance(result, dict), f"agui_envelope({kind!r}) returned {type(result)}"
        assert "type" in result, f"agui_envelope({kind!r}) missing 'type' key"


# ---------------------------------------------------------------------------
# Pipeline adapter
# ---------------------------------------------------------------------------

def test_pipeline_adapter_translates_known_events() -> None:
    """translate_pipeline_event must handle every documented event type."""
    from strands_agents.playground.pipeline_adapter import translate_pipeline_event

    known_events = [
        ("pipeline.run_started", {"topic": "test", "target_duration_sec": 60, "language": "en"}),
        ("pipeline.stage_started", {"stage": "scenario", "scene_count": 3}),
        ("pipeline.stage_finished", {"stage": "scenario", "elapsed_ms": 1000}),
        ("pipeline.tool_call_started", {"tool": "generate_scenario", "agent": "scenario", "args_summary": "{}"}),
        ("pipeline.tool_call_finished", {"tool": "generate_scenario", "agent": "scenario", "elapsed_ms": 500, "ok": True}),
        ("pipeline.approval_gate", {"gate_name": "scenario_review", "allowed_decisions": ["approve", "reject"]}),
        ("pipeline.approval_resumed", {"gate_name": "scenario_review", "decision": "approve"}),
        ("pipeline.artifact", {"kind": "narration", "scene_num": 1, "revision_tag": "v1"}),
        ("pipeline.stage_failed", {"stage": "visual", "reason": "timeout"}),
        ("pipeline.run_finished", {"status": "ok"}),
    ]

    for event_type, data in known_events:
        result = translate_pipeline_event(event_type, data)
        assert result.kind is not None and len(result.kind) > 0, (
            f"translate_pipeline_event({event_type!r}) returned empty kind"
        )
        assert result.summary is not None and len(result.summary) > 0, (
            f"translate_pipeline_event({event_type!r}) returned empty summary"
        )


def test_pipeline_adapter_unknown_event_does_not_drop() -> None:
    """Unknown orchestrator events must translate to pipeline.unknown."""
    from strands_agents.playground.pipeline_adapter import translate_pipeline_event

    result = translate_pipeline_event(
        "pipeline.new_event_type", {"foo": "bar"}
    )
    assert result.kind == "pipeline.unknown", f"expected pipeline.unknown, got {result.kind!r}"
    assert result.detail.get("source_event_type") == "pipeline.new_event_type", (
        f"expected source_event_type='pipeline.new_event_type', got {result.detail.get('source_event_type')!r}"
    )
