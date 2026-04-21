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

from strands_agents.evals.experiments.content_analyst import (
    CONTENT_ANALYST_EVALUATOR_THRESHOLDS,
    build_content_analyst_experiment,
    content_analyst_cases,
)
from strands_agents.evals.experiments.scenario import (
    SCENARIO_EVALUATOR_THRESHOLDS,
    build_scenario_experiment,
    scenario_cases,
)
from strands_agents.evals.experiments.visual_concepter import (
    VISUAL_CONCEPTER_EVALUATOR_THRESHOLDS,
    build_visual_concepter_experiment,
    visual_concepter_cases,
)

__all__ = [
    "CONTENT_ANALYST_EVALUATOR_THRESHOLDS",
    "SCENARIO_EVALUATOR_THRESHOLDS",
    "VISUAL_CONCEPTER_EVALUATOR_THRESHOLDS",
    "build_content_analyst_experiment",
    "build_scenario_experiment",
    "build_visual_concepter_experiment",
    "content_analyst_cases",
    "scenario_cases",
    "visual_concepter_cases",
]
