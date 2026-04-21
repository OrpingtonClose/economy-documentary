"""Experiment factories for strands_agents evals.

Each component PR adds a module here exporting a ``build_*`` factory
and a ``*_task`` callable, keeping experiment construction colocated
with the agent it tests.
"""

from __future__ import annotations

from .pipeline import build_pipeline_experiment, pipeline_task

__all__ = [
    "build_pipeline_experiment",
    "pipeline_task",
]
