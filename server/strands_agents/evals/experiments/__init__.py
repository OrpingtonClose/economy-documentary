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
from strands_agents.evals.experiments.production import (
    PRODUCTION_EVALUATOR_THRESHOLDS,
    build_production_experiment,
    production_cases,
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
    "PRODUCTION_EVALUATOR_THRESHOLDS",
    "SCENARIO_EVALUATOR_THRESHOLDS",
    "SCENARIO_REFINER_EVALUATOR_THRESHOLDS",
    "TIMING_EVALUATOR_THRESHOLDS",
    "TIMING_LOOP_EVALUATOR_THRESHOLDS",
    "VISUAL_CONCEPTER_EVALUATOR_THRESHOLDS",
    "VISUAL_LOOP_EVALUATOR_THRESHOLDS",
    "assembly_task",
    "audio_cases",
    "build_assembly_experiment",
    "build_audio_experiment",
    "build_coherence_evaluator_experiment",
    "build_content_analyst_experiment",
    "build_production_experiment",
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
    "production_cases",
    "refiner_cases",
    "scenario_cases",
    "smoke_task",
    "timing_cases",
    "timing_loop_cases",
    "visual_concepter_cases",
    "visual_loop_cases",
]
