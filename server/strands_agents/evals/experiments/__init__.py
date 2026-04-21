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

from strands_agents.evals.experiments.scenario import (
    SCENARIO_EVALUATOR_THRESHOLDS,
    build_scenario_experiment,
    scenario_cases,
)

__all__ = [
    "SCENARIO_EVALUATOR_THRESHOLDS",
    "build_scenario_experiment",
    "scenario_cases",
]
