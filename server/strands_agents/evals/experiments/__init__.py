"""Strands experiment factories.

One submodule per component. Each exports a ``build_experiment()``
callable returning a fully-assembled :class:`strands_evals.Experiment`
whose cases, evaluators, and metadata mirror the component spec under
``docs/strands-migration/components/``.

Experiment factories are deliberately thin; they assemble cases +
evaluators but do **not** execute any LLM / tool call themselves. The
caller (CI runner, notebook, shadow job) supplies the ``task`` callable
to :meth:`Experiment.run_evaluations`.
"""

from __future__ import annotations

from strands_agents.evals.experiments.approval import (
    approval_task,
    build_approval_experiment,
)
from strands_agents.evals.experiments.assembly import (
    ASSEMBLY_EXPERIMENT_NAME,
    assembly_task,
    build_assembly_experiment,
    cleanup_assembly_artifact_root,
)
from strands_agents.evals.experiments.audio import (
    AUDIO_EVALUATOR_THRESHOLDS,
    audio_cases,
    build_audio_experiment,
)
from strands_agents.evals.experiments.coherence_evaluator import (
    COHERENCE_EVALUATOR_THRESHOLDS,
    build_coherence_evaluator_experiment,
    coherence_evaluator_cases,
)
from strands_agents.evals.experiments.content_analyst import (
    CONTENT_ANALYST_EVALUATOR_THRESHOLDS,
    build_content_analyst_experiment,
    content_analyst_cases,
)
from strands_agents.evals.experiments.infra_agent import (
    INFRA_AGENT_EVALUATOR_THRESHOLDS,
    build_infra_agent_experiment,
    infra_agent_cases,
    infra_agent_task,
)
from strands_agents.evals.experiments.infra_b2_checkpoint import (
    INFRA_B2_CHECKPOINT_EVALUATOR_THRESHOLDS,
    build_infra_b2_checkpoint_experiment,
    infra_b2_checkpoint_cases,
    infra_b2_checkpoint_task,
)
from strands_agents.evals.experiments.infra_guardian import (
    INFRA_GUARDIAN_EVALUATOR_THRESHOLDS,
    build_infra_guardian_experiment,
    infra_guardian_cases,
    infra_guardian_task,
)
from strands_agents.evals.experiments.infra_ltx_video_worker import (
    INFRA_LTX_VIDEO_WORKER_EVALUATOR_THRESHOLDS,
    build_infra_ltx_video_worker_experiment,
    infra_ltx_video_worker_cases,
    infra_ltx_video_worker_task,
)
from strands_agents.evals.experiments.infra_ltx_video_worker_live import (
    INFRA_LTX_VIDEO_WORKER_LIVE_EVALUATOR_THRESHOLDS,
    build_infra_ltx_video_worker_live_experiment,
    infra_ltx_video_worker_live_cases,
    infra_ltx_video_worker_live_task,
)
from strands_agents.evals.experiments.infra_qwen3_tts_worker import (
    INFRA_QWEN3_TTS_WORKER_EVALUATOR_THRESHOLDS,
    build_infra_qwen3_tts_worker_experiment,
    infra_qwen3_tts_worker_cases,
    infra_qwen3_tts_worker_task,
)
from strands_agents.evals.experiments.infra_worker_registry import (
    INFRA_WORKER_REGISTRY_EVALUATOR_THRESHOLDS,
    build_infra_worker_registry_experiment,
    infra_worker_registry_cases,
    infra_worker_registry_task,
)
from strands_agents.evals.experiments.escalation import (
    ActionEqualsEvaluator,
    HumanSummaryRequiredEvaluator,
    build_escalation_contract_experiment,
    build_escalation_experiment,
    build_escalation_judge_experiment,
    escalation_contract_task,
    escalation_judge_task,
    escalation_task,
)
from strands_agents.evals.experiments.pipeline import (
    build_pipeline_experiment,
    pipeline_task,
)
from strands_agents.evals.experiments.production import (
    PRODUCTION_EVALUATOR_THRESHOLDS,
    build_production_experiment,
    production_cases,
)
from strands_agents.evals.experiments.recovery import (
    RECOVERY_EXPERIMENT_NAME,
    build_recovery_classifier_contract_experiment,
    build_recovery_experiment,
    build_recovery_remanifester_contract_experiment,
    recovery_task,
)
from strands_agents.evals.experiments.scenario import (
    SCENARIO_EVALUATOR_THRESHOLDS,
    build_scenario_experiment,
    scenario_cases,
)
from strands_agents.evals.experiments.scenario_refiner import (
    SCENARIO_REFINER_EVALUATOR_THRESHOLDS,
    build_refiner_experiment,
    refiner_cases,
)
from strands_agents.evals.experiments.smoke import build_smoke_experiment, smoke_task
from strands_agents.evals.experiments.timing import (
    TIMING_EVALUATOR_THRESHOLDS,
    timing_cases,
)
from strands_agents.evals.experiments.timing import (
    build_experiment as build_timing_experiment,
)
from strands_agents.evals.experiments.timing_loop import (
    TIMING_LOOP_EVALUATOR_THRESHOLDS,
    build_timing_loop_experiment,
    timing_loop_cases,
)
from strands_agents.evals.experiments.visual_concepter import (
    VISUAL_CONCEPTER_EVALUATOR_THRESHOLDS,
    build_visual_concepter_experiment,
    visual_concepter_cases,
)
from strands_agents.evals.experiments.visual_loop import (
    VISUAL_LOOP_EVALUATOR_THRESHOLDS,
    build_visual_loop_experiment,
    visual_loop_cases,
)

__all__ = [
    "ASSEMBLY_EXPERIMENT_NAME",
    "AUDIO_EVALUATOR_THRESHOLDS",
    "COHERENCE_EVALUATOR_THRESHOLDS",
    "CONTENT_ANALYST_EVALUATOR_THRESHOLDS",
    "INFRA_AGENT_EVALUATOR_THRESHOLDS",
    "INFRA_B2_CHECKPOINT_EVALUATOR_THRESHOLDS",
    "INFRA_GUARDIAN_EVALUATOR_THRESHOLDS",
    "INFRA_LTX_VIDEO_WORKER_EVALUATOR_THRESHOLDS",
    "INFRA_LTX_VIDEO_WORKER_LIVE_EVALUATOR_THRESHOLDS",
    "INFRA_QWEN3_TTS_WORKER_EVALUATOR_THRESHOLDS",
    "INFRA_WORKER_REGISTRY_EVALUATOR_THRESHOLDS",
    "PRODUCTION_EVALUATOR_THRESHOLDS",
    "RECOVERY_EXPERIMENT_NAME",
    "SCENARIO_EVALUATOR_THRESHOLDS",
    "SCENARIO_REFINER_EVALUATOR_THRESHOLDS",
    "TIMING_EVALUATOR_THRESHOLDS",
    "TIMING_LOOP_EVALUATOR_THRESHOLDS",
    "VISUAL_CONCEPTER_EVALUATOR_THRESHOLDS",
    "VISUAL_LOOP_EVALUATOR_THRESHOLDS",
    "ActionEqualsEvaluator",
    "HumanSummaryRequiredEvaluator",
    "approval_task",
    "assembly_task",
    "audio_cases",
    "build_approval_experiment",
    "build_assembly_experiment",
    "build_audio_experiment",
    "build_coherence_evaluator_experiment",
    "build_content_analyst_experiment",
    "build_escalation_contract_experiment",
    "build_escalation_experiment",
    "build_escalation_judge_experiment",
    "build_infra_agent_experiment",
    "build_infra_b2_checkpoint_experiment",
    "build_infra_guardian_experiment",
    "build_infra_ltx_video_worker_experiment",
    "build_infra_ltx_video_worker_live_experiment",
    "build_infra_qwen3_tts_worker_experiment",
    "build_infra_worker_registry_experiment",
    "build_pipeline_experiment",
    "build_production_experiment",
    "build_recovery_classifier_contract_experiment",
    "build_recovery_experiment",
    "build_recovery_remanifester_contract_experiment",
    "build_refiner_experiment",
    "build_scenario_experiment",
    "build_smoke_experiment",
    "build_timing_experiment",
    "build_timing_loop_experiment",
    "build_visual_concepter_experiment",
    "build_visual_loop_experiment",
    "cleanup_assembly_artifact_root",
    "coherence_evaluator_cases",
    "content_analyst_cases",
    "escalation_contract_task",
    "escalation_judge_task",
    "escalation_task",
    "infra_agent_cases",
    "infra_agent_task",
    "infra_b2_checkpoint_cases",
    "infra_b2_checkpoint_task",
    "infra_guardian_cases",
    "infra_guardian_task",
    "infra_ltx_video_worker_cases",
    "infra_ltx_video_worker_live_cases",
    "infra_ltx_video_worker_live_task",
    "infra_ltx_video_worker_task",
    "infra_qwen3_tts_worker_cases",
    "infra_qwen3_tts_worker_task",
    "infra_worker_registry_cases",
    "infra_worker_registry_task",
    "pipeline_task",
    "production_cases",
    "recovery_task",
    "refiner_cases",
    "scenario_cases",
    "smoke_task",
    "timing_cases",
    "timing_loop_cases",
    "visual_concepter_cases",
    "visual_loop_cases",
]
